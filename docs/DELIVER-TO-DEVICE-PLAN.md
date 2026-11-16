# Plan: "Deliver to my device" mode (public, client-delivery)

Goal: run Encoreum as an Authentik-gated site (`encoreum.example.com`) where the
**server** downloads + processes, and the finished files are saved onto the **client's
machine** (the remote laptop), not the host's Plex library.

Hard constraint (drives everything): a browser cannot run yt-dlp/ffmpeg/streamlink, so
the server always does the download/processing; the bytes are then relayed to the client.
The existing LAN "save to the host library" behavior stays **untouched** — this is an
additive, opt-in second *sink*.

---

## 1. Architecture

Introduce a per-job **sink**:

| sink | writes to | who | exposure |
|------|-----------|-----|----------|
| `library` (today) | the host Plex library (`$MEDIADIR1/...`) | trusted LAN / admin | internal |
| `device` (new) | per-session ephemeral workspace → streamed to the client's chosen folder | any Authentik user | `encoreum.example.com` |

Delivery to the client, two mechanisms:
1. **File System Access API** (Chromium: Chrome/Edge/Opera) — user picks a local folder
   once; the app writes `Show/Season/Episode.mkv` + `.nfo` + artwork into it with structure
   intact. Closest to the vision.
2. **ZIP download** (universal fallback for Firefox/Safari) — server zips the job folder;
   browser downloads it; user unzips into their local library.

**Critical isolation:** device-sink jobs must be *incapable* of writing to the Plex
library. Enforced by a separate `ALLOWED_ROOTS` = the deliveries dir only (reuses the
existing `config.safe_join` guardrail). Device jobs also **skip the library staging**
logic (that exists only to protect the shared live Plex folder); they download directly
into their isolated, ephemeral per-session dir.

---

## 2. Backend changes

### 2.0 Roles & quotas (DECIDED)

Two Authentik groups drive everything:

| role (Authentik group) | may use | quotas |
|---|---|---|
| **`users`** | **device** sink only (download to their own machine) | 25 videos/hr · 6 GB/hr · 8 GB/day |
| **`superusers`** | device sink **+ library** sink (write the host's Plex library, incl. remotely) | none (or generous) |

- The `library` sink is **superuser-only** anywhere it isn't the LAN home box. `users`
  can never write the Plex library.
- **Rolling windows** (per user, sliding): 25 items and 6 GB in the last **1 h**; 8 GB in
  the last **24 h**. Enforced at create-time from the preview's item count + size estimate,
  and reconciled with actual bytes on completion.
- **Cache lifetime:** delivered files live in the cache for **up to 2 h**, then auto-purge.
  A **"Clear now"** button lets a user free their cache immediately after a successful
  download (voluntary, courteous — frees server disk sooner). Clearing frees *disk*; the
  rolling *download* counters still reflect what was fetched.

### 2.1 config.py
- `CACHE_DIR` — **`/mnt/nas/.encoreum-cache`** (a sibling of `media/`, fully **outside**
  the Plex media tree so Plex never scans it, on the roomy NAS disk). Mounted into the
  container as `/cache`. Layout: `/cache/<user>/<job_id>/<show>/Season .../...`.
- Quota/guardrail constants (env-overridable), matching §2.0:
  - `USER_MAX_ITEMS_PER_HOUR = 25`
  - `USER_MAX_GB_PER_HOUR = 6`
  - `USER_MAX_GB_PER_DAY = 8`
  - `DEVICE_MAX_CONCURRENT = 1` (concurrent device jobs per user)
  - `DEVICE_JOB_TIMEOUT = 3600` s
  - `CACHE_TTL = 7200` s (2 h)
  - `CACHE_DISK_CAP_GB = 50` (global) — refuse new jobs if `/cache` exceeds
  - `URL_ALLOWLIST` — music-performance platform list (§7.3); `users` only
  - `BLOCK_PRIVATE_IPS = true` — unconditional SSRF guard (all roles)
- `resolve_device_dir(user, job_id, show)` validated against a **device-only ALLOWED_ROOTS
  = [`/cache`]** — structurally impossible for a device job to reach the media library.
- A per-user **usage ledger** table `usage(user, ts, items, bytes, sink)`; rolling-window
  sums answer "can this user start this job right now?".

### 2.2 Auth / identity
- Behind Authentik forward-auth (chain-authentik@file), the app reads:
  - `X-authentik-username` → `user`
  - `X-authentik-groups` → role (`superusers` if present, else `users`; reject if in
    neither group).
- `current_user(request)` dependency returns `(user, role)`; device routes require a role;
  library-sink jobs require `superusers`.

### 2.3 Job model (jobs.py)
- Add columns `sink` (`library`|`device`) and `owner` (username; null for library/LAN).
- Device jobs: `show_dir = DELIVERIES_DIR/<user>/<job_id>/<show>`.
- Reuse the whole pipeline (download → rename/renumber → NFO → artwork) unchanged — it
  just runs against the isolated dir. `run_job` already takes `job_id`.

### 2.4 pipeline.py
- When `p["sink"] == "device"`: force `direct`-style flow (no library staging — the
  workspace is already private/ephemeral) and post-process in place.
- Enforce `DEVICE_MAX_GB` mid-run: periodic dir-size check; abort + mark job `error` if
  exceeded (belt to the preview-based pre-check).

### 2.5 Delivery endpoints (all require auth + own-job check)
- `GET  /api/deliver/{job_id}/manifest` → `[{path, bytes}]` (relative paths under the job
  dir) — the FS Access client iterates this.
- `GET  /deliver/{job_id}/file?path=<rel>` → streams one file (validated within the job
  dir). FS Access client writes it locally.
- `GET  /deliver/{job_id}/zip` → streams a ZIP of the job dir (fallback).
- `POST /api/deliver/{job_id}/done` → client acks delivery → server deletes the job dir.
- Ownership: `job.owner == current_user`; else 403. Path validated with `safe_join`
  against the job dir (no `../`).

### 2.6 Quotas / lifecycle enforcement
- **Create-time (rolling windows):** preview gives item count + est. size. Reject if, in
  the last hour, `items + est_items > 25` or `bytes + est_bytes > 6 GB`; or in the last
  day, `bytes + est_bytes > 8 GB`; or user already at `DEVICE_MAX_CONCURRENT`; or `/cache`
  over `CACHE_DISK_CAP_GB`. Clear, friendly rejection ("You've used 5.2/8 GB today — try a
  smaller set or wait N h").
- **Completion:** write the actual `(user, ts, items, bytes)` to the usage ledger.
- **Run-time:** `DEVICE_JOB_TIMEOUT` kills over-long jobs; a periodic size check aborts a
  job that blows past its estimate.
- **Cache TTL sweep:** APScheduler task purges job dirs older than `CACHE_TTL` (2 h) and
  orphaned per-user dirs. **"Clear now"** (`POST /api/deliver/{job}/done`) purges early.
- **Rate limit:** Traefik `chain-authentik` already includes rate-limit middleware.

### 2.7 `metadata_only` job mode
Builds the Plex scaffolding from a platform's **public catalog data** — no video, no DRM
touched. Intended to pair with a copy the user already owns through a lawful channel (e.g.
a disc they ripped for personal backup) or to enrich an existing library item.

- **Trigger:** auto-suggested when a URL matches `META_ONLY_HOSTS`; also selectable as a
  checkbox for any URL ("Metadata + artwork only, no video").
- **Pipeline:** `yt-dlp --skip-download --write-info-json --write-thumbnail
  --convert-thumbnails jpg` → then the existing `generate-nfo.py` + `generate-plex-art.py`
  produce `tvshow.nfo` / `season.nfo` / episode `.nfo` + poster/fanart, named with the
  chosen scheme (sequence/datecode/…). **No `.mkv` is created.**
- **Result:** a ready-to-fill folder — the user drops their own, legally-obtained video
  file (named to match) beside the `.nfo`, and Plex shows a complete episode. Or, if the
  named video already exists in the library, run it there to enrich existing items.
- **Guardrails:** metadata-only jobs don't count against the GB quotas (tiny), still
  count against the items/hr limit; the log/UI is explicit that no protected media is
  fetched. If even metadata extraction is blocked, fail with a clear message.
- **Job field:** `metadata_only: bool`; when set, `_download` is replaced by the
  skip-download metadata fetch, then normal post-processing runs.

---

## 3. Frontend changes

### 3.1 Sink selector
- "Save to: ( ) My device  ( ) Server library" — on the external domain default to
  **device** (and hide/disable library unless admin); on LAN keep both.
- A **"Metadata + artwork only (no video)"** checkbox (§2.7), auto-suggested + explained
  when the URL is a recognized fully-DRM host; the compat/preview panel labels such hosts
  "DRM — video not downloadable" rather than erroring cryptically.
- Feature-detect FS Access API: `if (!window.showDirectoryPicker)` → device mode shows
  **only** the ZIP path with a note ("folder-saving needs Chrome/Edge").

### 3.2 File System Access flow (Chromium)
1. "Choose destination folder" → `dir = await showDirectoryPicker()` (store handle for
   the session; optionally `dir.requestPermission({mode:'readwrite'})`).
2. On job `done`: `GET manifest` → for each `{path}`:
   - split into segments; `getDirectoryHandle(seg, {create:true})` down the tree
   - `getFileHandle(name, {create:true})` → `createWritable()` → stream `GET file?path=`
     body into it → close.
3. Progress: "written N / total". On finish → `POST done` (server cleans up).
4. ZIP fallback button always present.

### 3.3 Live streams (device)
- Server captures to the ephemeral dir; on stop, deliver via the same manifest/file flow
  (or ZIP). No incremental folder-write during capture in v1.

---

## 4. Exposure (two-container split, reuses existing pattern)
- **Public instance** `encoreum-web` (device sink only, `/cache` only, no `/library`):
  - Traefik `appdata/traefik3/rules/the host/app-encoreum.yml` → backend
    `http://172.17.0.1:<web-port>`, middleware `chain-authentik@file`, `certResolver:
    dns-cloudflare` (no `options: tls-opts@file` — see docker CLAUDE.md 421 note).
  - Cloudflare Tunnel hostname `encoreum.example.com` (external opt-in, per the
    [[project_cloudflare_tunnel]] model). Caveat: CF Tunnel timeouts/size on huge ZIPs →
    **prefer per-file FS Access streaming** (many small requests) over one giant ZIP.
- **LAN instance** `encoreum` (today's): both sinks, `/library` mounted. Reached on LAN
  and over **Tailscale** for superuser remote library writes — **not** on the CF Tunnel.
- Only the public instance is internet-exposed; the library is never reachable from the
  public domain.

---

## 5. Security review checklist
- [ ] Every `/deliver/*` route authenticated + `owner == current_user`.
- [ ] Device sink `ALLOWED_ROOTS` = deliveries dir only → cannot write the media library.
- [ ] `safe_join` on every delivery path (no `../` escape out of the job dir).
- [ ] Quotas enforced (items, GB, concurrency, disk cap, timeout) — abuse / disk-fill.
- [ ] URL allowlist (youtube/vimeo/twitch) to shrink SSRF/abuse surface.
- [ ] Gated to an Authentik group; not world-open (ToS/legal — family use).
- [ ] **Advanced yt-dlp options passthrough** (`ytdlp_opts`): **available to standard
      `users`** (decision 2026-07-10 — full yt-dlp *download* features for everyone). Safety
      comes from a **denylist of non-download system hooks**, not from hiding the field:
      blocks flags that run programs (`--exec*`, `--use-postprocessor`, `--postprocessor-args`/
      `--ppa`, `--external-downloader`/`--downloader`/`--downloader-args`, `--plugin-dirs`) or
      read/write outside the job folder (`-o/--output`, `-P/--paths`, `--cache-dir`,
      `--ffmpeg-location`, `--cookies`, `--batch-file`, `--load-info-json`, `--config*`,
      `--download-archive`, `--home`, `--print-to-file`). These aren't download features, so
      users lose nothing. Predicated on **Authentik-family gating** (authenticated, not
      anonymous). If ever opened to untrusted users, switch to an *allowlist* of safe flags
      (a denylist can't guarantee coverage of every shell-out path). An in-UI **help toggle**
      explains how/why to use switches + what's blocked.
- [ ] Fully-DRM hosts are `META_ONLY_HOSTS` (metadata-only, §2.7); the app fails clearly
      rather than erroring cryptically. Metadata-only builds NFO/artwork for content the
      user owns elsewhere.
- [ ] Deliveries volume is separate + size-capped so it can never fill the host's disk.
- [ ] TTL cleanup verified; no PII/media lingers.
- [ ] Run `/security-review` on the diff before exposing.

---

## 6. Rollout phases
1. **Backend sink + isolation** — `sink=device`, per-user ephemeral dirs, device-only
   ALLOWED_ROOTS, manifest/file/zip/done endpoints, TTL sweep, quotas. Test on LAN
   (no auth yet) with a tiny playlist and a single live stream.
2. **Frontend delivery** — sink selector, FS Access API writer + progress, ZIP fallback,
   feature detection.
3. **Expose** — Authentik forward-auth + group, Traefik rule, Cloudflare Tunnel hostname;
   security review.
4. **Polish** — quota UX/messaging, live-stream delivery, per-user history, metrics.

---

## 7. Decisions

### 7.1 Cache location — DECIDED
`/mnt/nas/.encoreum-cache` → mounted `/cache`. Sibling of `media/`, outside every Plex
library path, on the NAS disk. `CACHE_DISK_CAP_GB = 50` global cap.

### 7.2 Who — DECIDED
Authentik groups `users` (device only, quotas of §2.0) and `superusers` (device + library,
no/loose quotas). Neither group → no access.

### 7.4 Public library writes — DECIDED
`library` sink is **superuser-only**. Recommended path: superusers reach the **LAN
instance over Tailscale** for library writes (they already have Tailscale) rather than
exposing library-write on the public internet — see 7.5.

### 7.3 URL allowlist — DECIDED
**Curated allowlist for `users`, covering platforms known for hosting music performances;
`superusers` bypass the allowlist; private/internal targets blocked for everyone.**

- **Unconditional (all roles):** reject any URL whose host resolves to a private /
  loopback / link-local IP (blocks SSRF at `127.0.0.1`, LAN ranges, `169.254.169.254`
  metadata). No exceptions.
- **`users` allowlist** (`URL_ALLOWLIST`, host-suffix match, expandable in config) —
  starting set of music-performance platforms yt-dlp supports:
  - **Video/live:** youtube.com, youtu.be, music.youtube.com, vimeo.com, twitch.tv,
    dailymotion.com, dai.ly, facebook.com, fb.watch, instagram.com, tiktok.com,
    bilibili.com, nicovideo.jp, rumble.com, kick.com, x.com, twitter.com, odysee.com
  - **Music-specific:** soundcloud.com, bandcamp.com, mixcloud.com, audius.co, nugs.net,
    veeps.com, npr.org (Tiny Desk), kexp.org, boilerroom.tv
  - **Indie series & fan films (free / no login — work in device mode):** archive.org
    (Internet Archive — fan films, public-domain & old indie series), newgrounds.com,
    odysee.com. (`bitchute.com` supported but opt-in — mixed content reputation.)
- **Subscription/creator platforms** (need the user's login cookies → **LAN/superuser
  instance only**, not the cookieless public device instance): nebula.tv, floatplane.com,
  rokfin.com.
- **Recognized · metadata-only (fully-DRM, non-downloadable)** — a *third* tier, `META_ONLY_HOSTS`
  (e.g. dropout.tv, curiositystream.com, veeps.com, mubi.com, and similar). These pass the
  SSRF/recognition check but are **never** treated as downloadable video. Behaviour:
  - **Default:** refuse with a clear, specific message ("DRM-protected — the video can't be
    downloaded. You can run *metadata-only* to build Plex NFO + artwork for a copy you
    already own.") — no cryptic yt-dlp error.
  - **Opt-in `metadata_only` job mode** (§2.7): fetch *public* catalog data only (title,
    description, air date, series/episode, thumbnail — the metadata, **not** the protected
    stream) and generate the Plex scaffolding.
  - We never attempt to obtain the DRM-protected media; this tier exists for honest
    failure + metadata scaffolding + catching any non-DRM clips on the same host.
- **`superusers`:** any yt-dlp-supported site (still blocked from private IPs).
- Implementation: `urlparse` host → suffix-match against allowlist for `users`; resolve
  host to IP(s) and reject private ranges for everyone.
  - **Federated caveat (PeerTube etc.):** these span arbitrary domains, so host-suffix
    can't whitelist them. Better long-term mechanism = **allow by yt-dlp extractor key**
    (`--print extractor_key`, or the extractor's `_VALID_URL`), which covers PeerTube/
    Mastodon-video for `users` too. v1 = host-suffix (covers the named platforms);
    extractor-based matching is the follow-up. Non-allowlisted URLs remain superuser-only.

### 7.5 Container topology — DECIDED
**Two-container split; superusers reach the library over Tailscale.**
- **LAN instance** (today's `encoreum`): `/library` mounted, both sinks, home + superuser
  use. Reachable on LAN and over **Tailscale**, so superusers get remote **library** writes
  with **zero public exposure** of the library.
- **Public instance** `encoreum-web` (`encoreum.example.com`): a second container from
  the same image, **only `/cache` mounted — no `/library` mount at all**,
  `ALLOW_LIBRARY_SINK=false`, device sink only. If it's ever compromised it **structurally
  cannot touch the Plex library** — the mount doesn't exist. Own `container_name`,
  `restart: unless-stopped`, its own rate-limit/quota, behind chain-authentik.
- The two share nothing except the code image; the public one has its own `/config` (jobs,
  usage ledger) and the `/cache` volume. Only the public one is on the Cloudflare Tunnel.

---

## 8. Effort estimate
- Phase 1 (backend): ~1 focused session. Phase 2 (frontend FS Access): ~1 session.
  Phase 3 (expose + security review): ~half session. The pipeline itself is reused as-is,
  so most work is the isolation/quota/delivery layer + the browser writer.
