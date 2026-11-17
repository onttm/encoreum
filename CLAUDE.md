# CLAUDE.md — Encoreum project instructions

Read this before writing any code in this repo.

Repo: `git@github.com:onttm/encoreum.git` · Local: `~/repos/encoreum`
Runs on **the host** as container `encoreum` on **:8681**. Created 2026-08-21.

> ## NAME: this ships as **Encoreum** (third name — read before renaming again)
>
> 1. **`concert-dl`**, 2026-08-21: a description, not a name. The eventual name
>    **Orpheum** was recorded the same day (commit cbe5f9b) and deferred.
> 2. **Orpheum**, 2026-10-05: renamed early because audiobook features made
>    "concert-dl" wrong. Repo, dir, container, image, cron watcher, all mentions.
> 3. **Encoreum**, 2026-11-16: a public project, `kstenerud/orpheum` (a Go
>    media server for films/series, orpheum-app.com), adopted "Orpheum" on
>    2026-09-23 after a name screen that could not see this then-private repo.
>    Its screen also found Bose Pro ORPHEUM marks (class 9 audio), theatre marks,
>    and the "Orpheus" misspelling leading to a music tracker and a stream
>    ripper — poor company for a download tool. With going public planned,
>    renamed. Encoreum = encore + "-eum" (hall for music, as in odeum/Orpheum);
>    screened 2026-11-16: no GitHub repo, no PyPI/npm/crates/Docker Hub name,
>    no USPTO mark, all checked domains unregistered.
>
> Same pass as before: GitHub repo (old URLs redirect), `~/repos/encoreum`,
> container + image, `/etc/cron.d/encoreum-watch` (+ /var/lib, /var/log),
> host group `encoreum-secret` (gid 985 unchanged), secret files
> `encoreum_*`, staging dir `.encoreum-staging`, and every in-repo mention.
> `ORPHEUM_*` env vars are still read as fallbacks. The Traefik route answers
> `encoreum.` AND the old `orpheum.` hostname so bookmarks keep working.
> **Still unchanged on purpose**: the port `:8681` and the library subfolder
> names (`audiobooks`/`concerts`/…).
>
> **Keep the commit history.** The bodies carry the reasoning, not just the
> changes (why `live_status` and not `is_live`; that Compose secret uid/gid/mode
> are Swarm-only; that YouTube RSS sends no ETag so 304s cannot happen; that an
> inner join silently hid every festival-set request). A squashed "Encoreum 1.0"
> commit is a tidier front page and a worse artifact, and it works against the
> inheritability goal. A clean release is a **tag plus release notes**, not a
> rewritten history. If a clean public face is wanted, publish a squashed public
> repo and keep the full trail private.

---

## What this is

A self-hosted **web UI for yt-dlp** that pulls concert videos into a Plex
*"Concerts as TV"* library: download → rename by real concert date → write
Kodi/Plex NFO + artwork. FastAPI + Jinja templates, no JS framework.

It is the Dockerised form of the standalone scripts in `scripts/`, which the app
shells out to **unchanged**. Those scripts must keep working on their own.

### Standalone rule — non-negotiable
This app has **no Claude or Anthropic dependency** and must never gain one as a
core feature. It runs on its own runtime with its own venv. If any Claude-backed
feature is ever added it must be explicitly opt-in, billing-transparent, and
degrade to a clear manual path when absent. (Global rule; see `~/.claude/CLAUDE.md`.)

---

## Architecture

```
app/main.py       FastAPI routes, form parsing, option validation/denylist
app/pipeline.py   the actual work: download → post-process → publish
app/config.py     paths, ALLOWED_ROOTS guardrail, safe_join()
app/jobs.py       job queue + sqlite persistence      app/history.py  job history
app/library.py    library browsing                    app/preview.py  dry-run preview
app/settings.py   persisted UI settings               app/updater.py  self-updating yt-dlp
app/templates/    index.html (main form), job.html (live log)
scripts/          standalone helpers, called by pipeline.py — keep them runnable alone
  analyze-channel.py   infer a channel's naming convention before downloading
  watch-channel.py     RSS watcher: catch scheduled live streams, no auth
```

**Data lives in `/config`** (bind-mounted `./config`, gitignored): `jobs.db`,
`logs/`, `archives/` (per-show yt-dlp download-archives — this is what makes
re-running a playlist resume-safe), and `venv/` (the self-updating yt-dlp).

### Two invariants — do not break these

1. **`config.safe_join()` is the single write choke-point.** Every path that
   gets written must pass through it. It resolves and then verifies the result
   is inside `ALLOWED_ROOTS`, raising on any `../` escape. Never build a write
   path with plain `Path` joins.

2. **Downloads stage, then publish.** yt-dlp writes into
   `LIBRARY_ROOT/.encoreum-staging/`, and `_publish()` moves completed files
   into the library with `os.replace` (same filesystem = atomic). Nothing
   half-written should ever appear in the Plex library. See the known bug below —
   this invariant is currently violated.

**File ownership**: `entrypoint.sh` drops to `PUID`/`PGID` so Plex can read what
lands in the library. On the host that is `1000:1000`.

---

## Naming modes

`main.py` accepts: `performance`, `sequence`, `datecode`, `episode`, `date`,
`audiobook` (default `performance`). Post-processing branches on this in
`pipeline.py:_post_process()`.

- `performance` → `scripts/name-by-performance.py`; drops empty fields cleanly,
  which is why a full concert with no songname still names correctly.
- `sequence` → `scripts/renumber-episodes.py`.

**Do not mix modes within one show.** `Band C/` currently has `Season 2019`,
`Season 2024`, `2023`, and `2025` — the product of switching modes mid-library.
Plex cannot reconcile both conventions in one series.

---

## Audio presets: Audiobook, Podcast, Music library (added 2026-09-25/26)

The five video naming modes are all "concert into a Plex TV library" — a
video, seasons/episodes, a Kodi NFO, Plex artwork. A YouTube playlist of an
audiobook/podcast/live-set read or performed chapter-by-chapter needs none of
that and a different destination per content type, so audio content is its
own AXIS (`app/presets.py`, `category: "audio"`) rather than a flag bolted
onto the video modes. All three audio presets share one mechanical download
path — flat folder, audio-only, numbered by true playlist position — and
differ only in destination. Adding a fourth means one dict entry in
`presets.py`, not touching `main.py`/`pipeline.py`/`preview.py` again.

| preset | `default_dest` | maps to |
|---|---|---|
| `audiobook` | `audiobooks` | `$MEDIADIR1/audiobooks` — Audiobookshelf's own mount (`docker/compose/the host/audiobookshelf.yml`), no new mount needed |
| `podcast` | `podcasts` | `$PODCASTSDIR` (`/mnt/nas/podcasts` by default) — a SEPARATE top-level host path, **nested inside** the `/library` mount at `/library/podcasts` in this repo's `docker-compose.yml` (see below) |
| `music` | `live-music` | `LIVE_AUDIO_DIR` — the exact folder `app/imports.py`'s approved taper uploads already land in; this preset and manual ingest now share one library |

- **Audio-only, no forced re-encode.** `-f bestaudio/best -x --audio-format
  best` picks whatever audio-only stream yt-dlp already has and only remuxes;
  it does not transcode Opus/AAC to a different codec. This is the same
  "lossy stays lossy" reasoning as `scripts/rip-concert-audio.py` elsewhere in
  this file — re-encoding a lossy source recovers nothing and only degrades
  it. The tradeoff: the output container varies by source (opus/m4a/webm),
  which is why the preview shows a `.<ext>` placeholder instead of a fixed
  extension.
- **One flat folder per show, numbered by TRUE playlist position**
  (`pipeline.flat_audio_template`, `%(playlist_index)03d`, shared by all three
  presets) — not a renumber-after-selection scheme like `episode` mode. A
  track's number never moves just because some other track was deselected.
- **No rename/NFO/artwork.** `pipeline._post_process()` returns immediately
  for any `naming in presets.AUDIO_NAMES` before any of the video branches,
  and `main._build_job_params()` forces `run_rename`/`run_nfo`/`run_artwork`
  to `False` regardless of what the (JS-dimmed) checkboxes say —
  belt-and-suspenders, since Audiobookshelf/the Live Music library read
  embedded tags and the folder itself; no Plex NFO agent is involved.
- **No live capture.** `_capture_live()` is a video-only path (streamlink →
  `.mkv`); an audio preset + `live=true` is rejected with a 400 rather than
  silently producing a video file in an audio folder.
- **The podcast mount is nested, not a new `ALLOWED_ROOTS` entry.** `$PODCASTSDIR`
  is a real separate top-level host path (unlike audiobooks/music, which both
  live under `$MEDIADIR1`), but `config.safe_join()`/`LIBRARY_ROOT` only know
  about ONE root. Rather than teach that code multiple named roots,
  `docker-compose.yml` mounts `$PODCASTSDIR` at `/library/podcasts` — Docker
  nests it inside the already-mounted `/library` volume, so "podcasts" is just
  an ordinary destination subfolder from the app's point of view, on-disk
  reality aside. Verified live: a real download landed in `/mnt/nas/podcasts/`
  on the host while the app only ever wrote to `/library/podcasts/`.
- **Requires `mutagen` in the yt-dlp venv.** `--embed-metadata` on an
  Ogg/Opus container (a real outcome of "no forced re-encode" above) goes
  through mutagen, not ffmpeg — unlike mp4/mp3/mkv, which ffmpeg tags
  natively. Without it the job actually completes (file downloaded and
  published) but yt-dlp's postprocessor step exits 1, and the job shows
  `error` despite having produced a good file. `entrypoint.sh` installs it
  into `$VENV` alongside yt-dlp on every container start so this never
  reappears after a fresh `/config` volume.
- **Known gap:** a single-video URL (not a playlist) has no
  `playlist_index`, so yt-dlp's own template renders literally `NA` in the
  filename instead of `001`. This mirrors the existing note above that
  `--playlist-items` is a no-op on single-video URLs — audio presets are
  built for playlists, like the rest of the app.
- **Deferred, not built:** per-preset METADATA suggestions (Author for
  audiobook; Venue/Date for music, reusing `name-by-performance.py`'s existing
  `parse_location`/`find_date` parsers). Needs new form fields and a decision
  on whether they're just informational or drive real tag-writing — see the
  `suggestions.metadata` key below, which exists in the contract and is
  currently always `{}`.

## "Audio only" checkbox — audio extraction on a VIDEO naming mode (added 2026-09-26)

Different from an audio preset. A preset (above) is its own content type: own
destination, own flat numbered layout, no rename/NFO/artwork at all. This
checkbox is available on the five VIDEO naming modes and does the opposite —
**keeps** that mode's dating/venue/destination logic (performance's tour+venue
naming, sequence's collision-free numbering, footage-date rename, the
`concerts`/whatever destination) and only swaps video-only concerns for
audio-only ones. Surfaced by a real ask: "I want this concert's normal
dating/organization, just as audio, not video."

`main._build_job_params()`'s `wants_audio_only = presets.is_audio(naming) or
checkbox`, used ONLY to force off what's genuinely video-only — `run_nfo`,
`run_artwork`, live capture (`_capture_live()` is a video-only streamlink
path). **`run_rename` is deliberately NOT touched by the checkbox** — that's
what makes this different from an audio preset; rename/renumber keep running
on the video mode's own logic. `pipeline._common_opts(audio_only=...)` reads
`naming in presets.AUDIO_NAMES or p.get("audio_only")` — same audio-extraction
branch (`-f bestaudio/best -x --audio-format best`, no forced re-encode) as
the presets, just reached a second way.

**The blocker this surfaced, fixed at the source:** `rename-by-footage-date.py`
(and `name-by-performance.py`/`renumber-episodes.py`, which import its
`VIDEO_EXTS` for their own file-discovery glob) only ever looked for
`.mkv`/`.mp4`/etc. With audio output, that glob found **zero files** — the job
would report `done` having silently renamed nothing, upload-date names intact,
which is a much worse failure than an error (it looks like success). Fixed by
adding `AUDIO_EXTS` and a combined `MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS` in
`rename-by-footage-date.py`, and switching all three scripts' `rglob()` filters
to `MEDIA_EXTS`. Deliberately did NOT touch `generate-nfo.py`/
`generate-plex-art.py`'s own `VIDEO_EXTS` — those are genuinely Kodi/Plex-TV-
specific and are already skipped outright by `run_nfo`/`run_artwork` being
forced off, so leaving their glob as video-only is inert, not a gap.

**Output naming was already extension-agnostic** in all three scripts — each
builds its new filename from `v.suffix` (the actual downloaded file's real
extension), never a hardcoded `.mkv`. Only the *discovery* glob was the
blocker. Verified end-to-end: a real `datecode` + `audio_only` + `run_rename`
job found the real upload date (2005-04-24) and renamed the `.opus` file into
`Season 2005/... - S2005E0424 - ....opus` — the video mode's naming scheme,
audio content, correct real extension preserved.

**Preview parity**: every hardcoded `.mkv` in `preview.py`'s name-building
(`_name_for`, `_preview_performance`, `_preview_sequence`, `_preview_episode`)
now goes through `_ext(p)`, which returns the same `<ext>` placeholder the
audio-preset preview already used (no forced re-encode → real container
unknown ahead of time). `_extract()`'s format-probe also switches to
`-f bestaudio/best` when `audio_only` is set, so the "media" quality string
shows only the audio codec/bitrate instead of a misleading video+audio combo;
the Plex-compat probe is skipped for the same reason it's skipped for audio
presets.

**UI**: the checkbox is disabled/dimmed on an audio preset (redundant —
already audio-only), and checking it dims NFO/artwork/Live/Quality
(`applyAudioOnlyDimming()`) without touching `run_rename`/season/reverse,
mirroring the backend split exactly.

## Audiobook: Author/Title/mediafile folder layout (added 2026-09-26)

Audiobook previously used the same flat layout as podcast/music
(`audiobooks/<Show>/<Show> - 001 - ....ext`). A real ask changed this
specifically for audiobook: organize as `audiobooks/<Author>/<Title>/` instead
— the conventional audiobook-library shape, and closer to what
Audiobookshelf itself expects.

**Unconditional, not conditional on an Author being typed.** `main._build_job_params()`
always nests for `naming == "audiobook"` — `folder_id = f"{author or
_UNKNOWN_AUTHOR}/{show}"` — never a flat fallback. A folder CONTRACT that
changes shape depending on how much the user happened to type is worse than
one that's always the same shape with an obvious `(unknown author)`
placeholder when blank (matches `_UNTITLED`'s existing precedent).

**`show` stays the Title alone**, deliberately — it's still what
`pipeline.flat_audio_template` uses as the per-track filename prefix
(`{Title} - 001 - Chapter.ext`), and that function already takes `show` and
`show_dir` as two independent arguments, so NOTHING in `pipeline.py` needed to
change: only how `main.py` computes `show_dir` for this one preset. A new
`author` field travels alongside in `params` but pipeline.py never reads it —
it only ever mattered for path construction, which already happened by the
time `run_job()` sees it.

**Suggestions split channel from title for audiobook only.** Previously the
channel name was one of the candidates offered for the (single) Show field.
For audiobook, a narrator/publisher channel ("GreatAudiobooks") is rarely the
book's own title, so `preview._show_suggestions()` now skips the channel
candidate specifically when `naming == "audiobook"`, and a new
`preview._suggest_author()` offers that same channel value as an **Author**
suggestion instead, landing in `suggestions.metadata.author` (the key this
contract reserved back when metadata suggestions were deferred). Every other
preset still gets `metadata: {}`.

**Title cleanup**: `preview._clean_audiobook_title()` strips
"Full Audiobook"/"(Unabridged)"/"(Abridged)" decoration from the playlist-title
candidate — audiobook-only, reusing the same keyword set as
`_suggest_preset`'s Audiobook-content-type signal.

**Live path-rewrite, same mechanism as `previewShowToken`**: a new
`previewAuthorToken` (from the response's `author` field) lets clicking the
Author chip rewrite already-rendered preview paths in place
(`applyAuthorFill()`), exactly like `applyShowRename()` — no second `/preview`
call for either field.

**Known limitation**: the existing-folder fuzzy match still only browses the
TOP LEVEL of the destination (`library.browse(dest)`), so it won't look inside
an existing Author folder for a matching Title. Pre-existing flat audiobook
folders (downloaded before this change) still fuzzy-match fine since they're
already top-level; a Title match one level down under an Author folder
wouldn't currently be found. Not fixed here — flagged rather than silently
incomplete.

**Bug found by the user, same day**: the "Files go to" hint and the live
"does this folder already exist" check (`updateWriteHint()`/`checkShow()` ->
`/api/show-info`) kept showing the OLD flat path after Author shipped — Author
was being used correctly by the actual job, but nothing on screen reflected
it, so there was no way to tell from the UI that it was working. Fixed by
extracting `main._folder_id(naming, show, author)` — the exact nesting logic
`_build_job_params` already had — and using it from BOTH the real job path and
`/api/show-info` (now takes `naming`/`author` too), plus a hand-mirrored
`folderId()` in the client JS for the hint text. Three places needed the same
one-line rule; missing even one of them is exactly how this happened.

`/preview` no longer requires a Show name (`main._build_job_params(...,
require_show=False)` for that route only — `/jobs` still requires one via the
form's native `required` attribute). A blank Show previews under the
placeholder `main._UNTITLED = "(untitled)"`, purely for path display; nothing
is ever written by Preview regardless.

Every `/preview` response carries a `suggestions` key:
```jsonc
"suggestions": {
  "show": [
    {"kind": "existing", "value": "...", "label": "...", "confidence": "certain"},
    {"kind": "channel",  "value": "...", "label": "...", "confidence": "probable"},
    {"kind": "title",    "value": "...", "label": "...", "confidence": "probable"}
  ],
  "preset": {"value": "audiobook", "label": "Audiobook",
             "reason": "the channel/playlist title mentions an audiobook",
             "confidence": "probable"} | null,
  "metadata": {}   // {} for every preset except audiobook -- see "Author/Title" below
}
```
`confidence` reuses this app's existing `certain`/`probable`/`weak` vocabulary
from `captures.py`'s setlist grading rather than a fabricated score.

**UI placement (moved 2026-09-26):** the chip containers (`#presetSuggest`,
`#authorSuggest`, `#showSuggest`) live in one `#suggestArea` block directly
under the URL field, not down by the file list — a real ask, since the
original placement meant scrolling past Destination/Show/Season/Naming/
Quality/.../Preview to see suggestions that exist to help fill in exactly
those fields. Clicking a chip still works identically regardless of where in
the DOM it sits (plain `getElementById` references, not DOM proximity).

- **`kind: "channel"` / `"title"`** come free from the SAME flat-playlist call
  `suggest_show()` already made for the old fill-while-typing autofill
  (`/api/suggest-show`) — no extra yt-dlp call. yt-dlp prints the literal
  string `"NA"` for a template field with no value (e.g. `%(playlist_title)s`
  on a bare video URL); `preview._na()` normalises that to real emptiness
  everywhere it's parsed — caught by testing when "NA" showed up as a
  suggested show name.
- **`kind: "existing"`** fuzzy-matches candidates against
  `library.browse(destination)`'s real folder list via `difflib`, and — if
  found — is inserted first and wins over a same-value channel/title entry.
  This directly targets the naming-fragmentation failure mode already
  documented above (`Band C/Season 2019/2023/2024/2025`): steer toward reusing a
  folder before a near-duplicate gets created.
  **Both sides are normalised before comparing** (`preview._norm_for_match`):
  strip bracketed metadata (`[Author]`, `(Narrator)`, `{287mb}`) and a leading
  `YYYY - ` prefix. Measured against a real audiobook folder in this library —
  `1980 - New Tales of the Example Mythos [Author] (Narrator) 10.24.25
  {287mb}` — the raw-string ratio was 0.55 (below threshold, no match found)
  vs. 0.67 normalised (crosses it). Threshold: ≥0.92 → `certain`, 0.6–0.92 →
  `probable`, below 0.6 → not shown at all (a suggestion list full of noise
  defeats itself, same lesson this app already learned from setlist grading).
- **UI**: chips (`.suggest .chip` in `app.css`), reusing the existing
  `#recent`/`.chip` pattern rather than a `<select>` — picking one calls
  `applyShowRename()`, which sets Show and rewrites the already-rendered
  preview paths client-side (string-replace on `previewShowToken`, from the
  response's `show` field) with **no second `/preview` request**. Typing in
  Show never clears the chips (you can always type your own); changing ANY
  OTHER field (including the naming/preset dropdown) does, since a preset
  switch changes what destination/metadata the chips even mean.
- **Every suggested value is routable to EITHER Title or Author (added
  2026-09-26)**, regardless of which one `preview.py` recommended. Surfaced by
  a real case: Load URL fires with whatever naming is CURRENTLY selected
  (usually the default video preset, before anyone has picked Audiobook), so
  `_show_suggestions()`'s channel-vs-title split (channel -> Author,
  title -> Title, audiobook-only) hasn't happened yet — both arrive as
  generic "show" candidates, and a real playlist (channel "Example Narration Channel", title "Example Serial...") showed both as if they were
  interchangeable Title options. Rather than chase better guessing (there
  isn't a good keyword-only fix for "is this channel the author"), the
  frontend now renders every candidate — from `suggestions.show` AND
  `suggestions.metadata.author` merged into one list — with its own value
  chip (click = its recommended field, backward compatible) plus two always-
  present `.chip-mini` buttons, "Title" and "Author", that call
  `applyShowRename()`/`applyAuthorFill()` directly regardless of the value's
  `kind`. No backend change was needed — `preview.py`'s recommendation is
  still a sensible default, this only adds the override.
- **`preset` (added 2026-09-26, `preview._suggest_preset`)** is a content-type
  nudge — "This looks like an Audiobook — switch & preview again?" — surfaced
  after a real user hit the gap this closes: previewing an ordinary playlist
  and expecting the app to notice it was actually audio content. Deliberately
  narrow, on purpose:
  - Fires ONLY on strong keyword signals in the channel/playlist title —
    `audiobook`/`unabridged`/`narrated by`/`read by` → Audiobook,
    `podcast` → Podcast (weak) — reusing the SAME `suggest_show()` call the
    show-name chips already make, so it costs nothing extra.
  - **Deliberately does NOT suggest Music on a parsed venue/"Live at" match.**
    That phrase appears in the title of a huge share of ORDINARY CONCERT
    VIDEOS — this app's primary, default use case — so firing on it would
    make the nudge noise on most normal downloads. A parsed venue is evidence
    of "this is a live performance," not evidence of "you wanted audio only."
  - Verified against the real playlist that surfaced the gap
    (`Example Serial` — a serialized narrated-fiction show on a
    "Example Narration Channel" channel, 88 episodes): correctly suggests
    NOTHING, because it's genuinely ambiguous between Audiobook and Podcast
    under this app's three-way split and matches neither keyword set. Saying
    nothing is the correct behavior here, not a bug — matches this app's own
    prior lesson (Coachella/Lollapalooza channel classification) that a
    confident wrong guess is worse than an unclassified item.
  - Never auto-switches. Clicking "Switch & preview again" sets the
    naming/preset dropdown, calls `updateNamingUI()`, and re-runs Preview —
    it does NOT try to preserve the stale file list, since the destination
    and layout are both about to change anyway.

---

## Known bugs — verified, not yet fixed

**1. Partial files leak into the library on cancel.**
`pipeline.py:_publish()` skips `.part` and `.ytdl`, but **not** yt-dlp's
in-flight merge target `*.temp.mkv`. Cancelling mid-merge publishes a truncated
file next to the real one. A confirmed instance:
`Band C - S2019E0815 …Full Concert.temp.mkv` (309 MB / 2529 s) beside the complete
324 MB / 2657 s file. **Fix**: extend the skip set to `*.temp.*` and `*.f[0-9]*`.

**2. ~~Live detection collapses five states into a boolean.~~ FIXED 2026-08-21.**
`_is_actually_live()` is now `_live_state()` and reads `live_status`, not the
`is_live` boolean. Verified against a real scheduled stream, which reported
`live_status=is_upcoming` while `is_live=False` — so the old code would have
fallen through to downloading a video that does not exist yet. The five states:

| `live_status` | Correct action |
|---|---|
| `is_live` | streamlink capture |
| `post_live` | **wait** — YouTube is still processing; downloading now gets a partial |
| `was_live` | normal download, using the *real* date |
| `not_live` | normal download |
| `is_upcoming` | reject with a clear message |

`post_live` was the damaging one: `is_live` is already False there, so the app
downloaded a file YouTube had not finished writing. `run_job()` now branches on
all five, waits out `post_live`, and parks on `is_upcoming` until showtime.

Two things this needed:
- `--ignore-no-formats-error` on the probe. An upcoming stream has NO formats
  yet, so yt-dlp errors out without it. This is why the old code could not have
  been fixed by swapping the field alone.
- `JobControl.sleep()`, a **cancellable** sleep. Cancel previously only killed a
  running child process, and a job parked on a stream three days out has none —
  it would have ignored the Cancel button entirely. `cancel()` now sets an Event
  that wakes the sleeper.

**3. Seasons come from upload date, not performance date.**
This is why 1992 Band C concerts sit in `Season 2019`. The bracketed date is
usually right there in the title. Running with `run_rename: true` is what fixes
it — the last job ran with it `false`. Related: two different Phoenix 1992 shows
both landed as `S2019E0725`, a collision that resolves once real dates are used.

---

## Source-channel research (2026-08-21)

Five usable sources, and **each needs a different extraction strategy** — there
is no single parse that works everywhere:

| Channel | Artists | Chapters | Date+venue from |
|---|---|---|---|
| `@bandcconcerts` | one | yes, full setlists | description line 1 (structured) |
| `@bandafan` | one | yes | title, year precision only |
| `@HDPinkFloyd` | two (Gilmour ≠ Floyd) | none | title parenthetical |
| `@frontrowmusic` | many | none | title prefix + release name |
| `@MontrealRocks` | many | none | prose description + venue gazetteer |

**Skip** `@coachella` and `@lollapalooza` (promo/interviews; 1 concert in 24
sampled). `@AustinCityLimitsTV` is worth it for full episodes only.

Rules that held across every channel tested:
- **Negative markers must be evaluated first.** Without it, `HANORAH on playing
  live with friends` matches "live" and lands in the concerts library.
- **A venue gazetteer is the strongest signal** on city-focused channels — it is
  what catches `Travis Scott CIRCUS MAXIMUS TOUR Montreal Bell Centre`, which
  contains neither "live" nor "concert".
- **Duration is a tiebreaker, never a classifier.** A 31-minute Band C set is a
  real concert; a 29-minute interview is not music at all.
- **Sample deeply.** A 12-video sample of `@MontrealRocks` produced the wrong
  conclusion ("skip it"); a 200-video scan found Band D, Springsteen, Band B.

Proposed buckets: **Concert / Single / Compilation / Documentary**, plus a reject
for interviews. Not yet built.

---

## Scheduled live streams — no account required

The notification bell is not machine-readable, and authenticating yt-dlp is the
wrong answer: it means persisting a Google **session** cookie, which is
account-wide (not YouTube-scoped) and gets accounts banned for automation. None
of it is necessary.

Three anonymous pieces do the whole job:

1. **The bell** → the channel's public Atom feed,
   `https://www.youtube.com/feeds/videos.xml?channel_id=UC...`. No key, no quota,
   no account. It is also a plain HTTP GET, so it keeps working when yt-dlp is
   being throttled by YouTube's anti-bot measures.
2. **When it starts** → `live_status` + `release_timestamp` from yt-dlp.
3. **Recording it** → `run_job()` parks on `is_upcoming`, polls until `is_live`,
   then captures with streamlink (`--hls-live-restart` = from the beginning).

`scripts/watch-channel.py` ties 1 and 2 together and can POST straight to a
running instance:

```bash
./scripts/watch-channel.py @AustinCityLimitsTV                 # what is coming up
./scripts/watch-channel.py @AustinCityLimitsTV --queue \
    --show "Austin City Limits" --once                          # from cron
```

It polls `live_status` rather than trusting the clock, because scheduled start
times slip routinely.

## Polling YouTube without getting blocked

Researched 2026-08-21. **No published rate limit exists for either endpoint**, so
these are behavioural rules, not thresholds to optimise against.

**The RSS feed** (`/feeds/videos.xml`) sits outside the Data API quota system and
has no documented limit. Community norm for feed readers is 15–60 min per
channel.

**CORRECTED 2026-08-21 by measurement:** the endpoint returns **no ETag and no
Last-Modified**, so conditional requests can never produce a 304 here — an
earlier claim in this file that they would was wrong. What it *does* return is
`Cache-Control: public, max-age=900`. So the real saving is **not re-fetching at
all while the CDN copy is fresh**, and that also puts a principled floor under
the interval: asking more often than every 15 minutes returns byte-identical
content. `discovery.py` stores `fresh_until` per source and skips accordingly.

The tooling therefore:
- sends the conditional headers anyway (harmless, and correct if YouTube ever
  adds validators) but relies on **Cache-Control** for the actual saving;
- **jitters** every interval ±15%, because perfectly regular timing is itself a
  bot signal;
- backs off on HTTP 429 with an **escalating** wait — 1h, then 2h, then a 4h
  ceiling — resetting the moment a fetch comes back clean. This file previously
  claimed exponential backoff while the code applied a flat hour and retried
  straight back into the same wall; the code now does what this says.

Note the feed only carries the most recent ~15 entries (verified: exactly 15 for
ACL). On a high-volume channel that, not freshness, is the argument for a
tighter interval.

**yt-dlp metadata calls** are the expensive side — they hit the InnerTube player
API, which is where bot detection lives. There is no safe published number.
What is known: datacentre IPs get the strictest treatment, and a tripped limit
typically clears in about **an hour**.

**The key insight that lets us poll gently**: streamlink runs with
`--hls-live-restart`, so a capture beginning a minute late *still gets the stream
from its beginning*. There is nothing to gain from tight polling to catch the
exact instant — and a rate-limit trip costs an hour, which would lose the entire
broadcast. Both wait loops are therefore floored at ~60s and widen with distance:

| Time until start | Poll every |
|---|---|
| > 2h | 15 min |
| 15 min – 2h | 5 min |
| < 15 min | 60 s |

Measured against the real ACL stream 88.6h out: **380 calls total, 4.3/hour.**

Two traps this design avoids, both found by testing rather than reasoning:
- `_wait_for_scheduled` originally had a flat 26h ceiling. Streams get announced
  **days** ahead — the ACL test case was 3.7 days out — so it would have given up
  two days before showtime. The deadline now follows `release_timestamp` + 2h.
- `watch-channel.py --queue` defers queuing until a stream is within
  `--queue-within` (default 8h), so a job does not sit parked for four days
  holding a worker.

## Concert queue (phase 1 — built 2026-08-21)

"Seerr, for live concerts": the family adds channels, a poller finds upcoming
broadcasts, people request the ones they want, an approved request becomes an
ordinary job. **No second pipeline, and no credentials anywhere.**

```
app/catalog.py     sources / events / requests  (same SQLite file as jobs)
app/discovery.py   feed polling + live_status resolution + channel resolution
```

The poller runs on the **APScheduler instance already in `main.py`** — 30 min,
`jitter=300`, `coalesce=True`, `max_instances=1`.

**The split that keeps it cheap.** The public Atom feed carries title, video id,
description, **thumbnail** and view count. So the whole browse UI renders with
zero yt-dlp calls. yt-dlp is asked only the one question a feed cannot answer —
*is this a scheduled stream, and when?* — and only for unsettled events, bounded
by `status_budget` per pass.

Measured steady state for one watched channel: **`cached: 1, checked: 1`** —
no HTTP request at all, and a single yt-dlp call for the one upcoming entry.
Settled events (`was_live` / `not_live`) are never re-checked; `is_upcoming` ones
always are, because they flip to `is_live` **without the feed changing**.

Endpoints (LAN-only, **no auth yet** — must be gated before exposure):
```
GET    /api/queue/stats      counts + poller health (next_poll)
GET    /api/queue/sources    POST to add {channel, show, ephemeral, auto_record}
DELETE /api/queue/sources/{id}
GET    /api/queue/upcoming   what the browse page will render
POST   /api/queue/poll       run a pass now
```

`/api/queue/stats` reports `poller.next_poll` on purpose: a discovery service
whose timer silently failed to register looks exactly like one with nothing to
report.

`sources.ephemeral` encodes the product thesis — capture value is inverse to
rehost likelihood. Auto-record should only ever default on for that tier.

## Artist matching — assume it will be wrong sometimes

`scripts/artist-lookup.py` resolves a name to a MusicBrainz artist via Lidarr,
then asks Plex what the library holds. It is **name-based, and names are
ambiguous**: "Levity" is a Polish jazz band, an EDM trio *and* a Brighton
4-piece; "Band C" is an American rock band, a Seattle band and a 90s trance DJ.

Ranking prefers artists already tracked in Lidarr, then exact name, then
has-genres — and it always prints the runners-up, so a wrong pick is visible
rather than silent. That is mitigation, not a fix.

**There must always be a human override, and corrections must persist.**
Fixing the same name twice is a bug.

```bash
./scripts/artist-lookup.py "Levity"                       # shows alternatives + a fix hint
./scripts/artist-lookup.py --fix "Levity" <mbid> --fix-note "EDM trio, not the jazz band"
./scripts/artist-lookup.py --list-fixes
```

Overrides live in `$CONFIG_DIR/artist-overrides.json` (`ARTIST_OVERRIDES` to
move it), short-circuit ranking entirely, and render as
`[MANUALLY CORRECTED]` so a corrected match never looks like a lucky guess.

**TODO(ui):** the browse page needs a **"Fix match"** control beside any
resolved artist — the affordance Plex, Sonarr and Radarr all have, for the same
reason. It must write to the same store as the CLI so the two never disagree.

**Consider scoping overrides per festival later.** "Levity" at Coachella is the
EDM trio; at a jazz festival it very likely is not. A global override is right
for now and will eventually be too blunt.

Two measured limits worth knowing before trusting a result:
- **~10% of artists have no MusicBrainz genres** (33 of the 332 Lidarr tracks).
  The lookup still identifies them; it just cannot say what they sound like.
- **The `Music Video` library uses agent `tv.plex.agents.none`** — no metadata
  at all, so matching there is filename-only.

## Ingest review queue — the manual/auto asymmetry

Built 2026-08-21. `app/imports.py` + `scripts/ingest-taper.py`.

**A manual upload is never written to the library without a person saying yes.**
Not "not written when confidence is low" — never. Confidence is the wrong gate,
because a *confident wrong match* is precisely the case review exists to catch:
the tagger can be certain twelve files map to twelve titles and still be certain
about the wrong show. Origin decides eligibility, so it is a `return False` in
`auto_importable()` and not a config lookup someone can flip:

| origin | how it got here | unattended import? |
|---|---|---|
| `manual` | dropped in `INGEST_DIR`, or uploaded | **never**, at any confidence |
| `auto` | a capture this app scheduled and ran | yes, when alignment is `exact` |

The asymmetry is the point. An automated capture already knows what it recorded
— it chose the artist, the date and the stream — so there is no outside claim to
check. A manual upload carries a claim from somewhere else, and checking it is
the human's job.

**Everything up to the decision is automated.** A 10-minute scan finds drops,
unpacks, reads info files and embedded tags, parses setlists, aligns them
against real audio durations and proposes artist/date/venue/source. Review is
one yes/no on a finished proposal, never a form to fill in.

**Overrides are stored apart from the proposal** and re-applied on every
re-inspect, so corrections survive a rescan and `corrected_fields` keeps showing
which values a human touched. Passing `""` clears a field back to the parser.
Correcting re-aligns *immediately* — the person fixing the match sees the
corrected mapping before anything is written, rather than discovering after the
fact that their fix made it worse.

```
POST /api/imports/scan              look for drops now (never writes to library)
GET  /api/imports?status=pending    the review queue
POST /api/imports/{id}/fix          artist/date/venue/city/source_type/setlist
POST /api/imports/{id}/approve      the ONLY path that writes to the library
POST /api/imports/{id}/reject
```

Guards that exist because the failure is silent otherwise:
- **Still-copying uploads are skipped.** The fingerprint is re-taken after a
  beat; if it moved, the drop is left for the next pass. Inspecting a
  half-transferred folder yields a truncated tracklist that looks entirely
  confident.
- **A decided import is never resurrected** by a rescan, even if it changed on
  disk — that would re-propose over rejections and corrections.
- **`approve()` requires a reviewer name.** "Who approved this" is the first
  question asked when a wrong album turns up.
- **One `_override_args()` builds flags for both the preview and the write.**
  They drifted apart exactly once: the preview honoured a corrected city, the
  write did not, so the queue displayed a field as fixed while the file kept the
  parser's guess. `--city` and `--source-type` were missing from the script CLI
  entirely.

`INGEST_DIR` is mounted **outside** the library. Nothing awaiting review is ever
visible to Plex.

## Festival metadata arrives in TWO stages

Built 2026-08-21. `app/captures.py` + `app/setlistfm.py`, hooked into
`pipeline.run_job()`.

A set is recorded while it airs, and at that moment **no setlist exists** —
they are written up by attendees hours to days later, and a large share of
festival sets never get one at all. So metadata cannot be one event:

| stage | when | what |
|---|---|---|
| 1 `minimal` | capture completes | artist, festival, stage, performance date, set times, venue, stream |
| 2 `enriched` | if/when posted | song-by-song setlist, tour, confirmed venue/city |

**`minimal` is a COMPLETE state, not a broken one.** The file is named, filed
and playable; everything in stage 1 came from what the app itself chose when it
pressed record, so none of it is a guess. Most captures stay `minimal` forever
and that is the expected outcome — never render it as an error or a warning.
`unavailable` is likewise terminal and normal: "nobody wrote this one up".

**Retries run for a week, then stop for good.**
`RETRY_HOURS = (6, 12, 18, 24, 36, 48, 72, 96, 168)` — nine attempts from 6h to
7 days, scheduled from the END of the set rather than from now, so a capture
that finishes early does not ask before the show is over. 6h/12h catch the
same-night write-ups, 18–48h the next-day ones, 72–168h the stragglers.

Past a week automatic polling **stops permanently** and the capture settles at
`unavailable`. That is a deliberate end, not a failure: a setlist that does not
exist after seven days almost certainly never will, and continuing to ask spends
a shared budget on the least likely results. **From then on it is manual** —
paste one in at any time, or `POST /api/captures/{id}/enrich` to ask once on
demand. Correcting the artist or date still reopens it automatically.

**The date trap.** setlist.fm keys on the date the show was **PLAYED**, never
the date it aired, and the format is `dd-MM-yyyy` — not ISO. A set ending 23:40
in Indio is already *tomorrow* in UTC, so a UTC-derived date misses by a full
day and returns nothing, which looks identical to "no setlist yet" forever.
`captures.local_date(epoch, festival_tz)` exists for this and
`performance_date` is stored explicitly rather than derived.

**Enrichment may never overwrite a human.** Corrections live in `overrides` and
beat both stages permanently. Two consequences:
- Correcting the artist **drops the mbid** for that lookup — an mbid resolved
  from the wrong name would otherwise silently outrank the correction.
- Correcting artist or performance date **reopens** a lookup that already gave
  up, because a previous "not found" said nothing about the corrected key.
  Without this, a capture that gave up under a wrong name stays `unavailable`
  forever for a show that is on setlist.fm.

### Only a CERTAIN match applies itself

A found setlist is graded before it is used, and only `certain` is written
unattended. Everything else is held as a **proposal** and marked for a person —
`setlist` (applied) and `proposed_setlist` (waiting) are separate columns so
nothing downstream can mistake one for the other.

| grade | meaning | action |
|---|---|---|
| `certain` | identity established AND one candidate AND nothing contradicts | applied automatically |
| `probable` | nothing contradicts, but identity or uniqueness is unconfirmed | held, tagged for review |
| `weak` | something actively disagrees (date, venue, artist, empty setlist) | held, tagged for review |

**Contradictions block; missing facts do not.** The first version required a
MusicBrainz ID match and treated a *missing* mbid as a failure — conflating "we
do not know" with "it disagrees", the very distinction the venue check gets
right. Since the mbid only ever comes from Lidarr enrichment and plain downloads
never carry one, that would have sent nearly everything to review permanently.
That is not a safe default but a broken one: **a review queue full of obviously
correct matches gets rubber-stamped, and a rubber-stamped queue catches
nothing** — too strict and too lax fail in the same place by different routes.

So identity can be established two ways, because there is more than one way to
know who played:
- the artist's MusicBrainz ID matches — identity is a fact; or
- the name matches **exactly** and the place agrees (the venue, or the festival
  named in setlist.fm's free-text `info`) — a different band with the same name
  at the same venue on the same day is not a real scenario.

A name match with nothing corroborating it stays `probable`: "Band C" is an
American rock band, a Seattle band and a 90s trance DJ.

Declining a proposal **remembers its setlist id**, so the same wrong match is
not re-offered on every later pass — otherwise the review queue is unclearable.
Confirming lands it as `manual`, not `enriched`: a person vouched for it, so no
later lookup may revisit it. `enriched` means the machine was certain alone.

### The mark

Every capture carries `setlist_match` — `matched` / `matched_by_hand` /
`needs_review` / `unmatched` — plus a plain `unmatched` boolean, so a download
with no setlist is visible at a glance instead of something to go looking for.
`GET /api/captures/unmatched` is that list; `stats()` gives the headline counts.
`needs_review` counts as unmatched: from the library's point of view a found-
but-unconfirmed setlist and no setlist are the same thing.

### Applying for the key

The form text is written down in **`docs/setlistfm-api-application.md`** —
including what Encoreum does with the data, what it deliberately does not do, and
the request volumes. Apply at <https://www.setlist.fm/settings/api> (logged in).

That file is a description of **actual behaviour**, and every claim in it is
verified against the code. If any of these change, change the document too or it
becomes a false statement made to a service that can revoke the key:
non-commercial, no bulk/crawl access, attribution displayed, no redistribution,
6s/1,000-per-24h volume, polling stops after a week.

Use the name **Encoreum** on the form. The key itself was issued in August under
an earlier name (concert-dl/Orpheum); if setlist.fm/settings/api still shows
that name, update it there by hand — nothing in this repo can.

### Attribution

Required by setlist.fm's licence, not a courtesy. `setlistfm.ATTRIBUTION` is one
constant used everywhere, and it is written **into the NFO** — both human-
readable in the plot and as a `<tag>` — so it travels with the file rather than
living in a config file or a UI footer that does not survive a rebuild or a
handover. Hand-pasted setlists do NOT get the setlist.fm credit unless a
setlist.fm URL was supplied with them.

**Optional by construction.** No API key required. `setlistfm.available()`
reports `api` / `manual` / `backoff` and the UI must show which. Without a key
the paste path produces the identical structure, and lands as `manual` — which
is excluded from all later lookups, because a person typed it.

`tape` tracks are dropped (recorded music over the PA is not a performance,
per setlist.fm's own guidelines) and every stored setlist keeps its `url`,
because attribution is a condition of the API terms — so it goes in the NFO
itself, not a config file that can be lost.

**Live captures need their own hook.** `_capture_live()` writes into the library
directly and never passes through `_publish()`, so it reports its own output in
`p["_captured"]`. That IS the festival path — missing it would have meant
festival sets were the one case that never got stage-2 metadata.

```
GET  /api/captures?state=minimal      what is waiting on a setlist
POST /api/captures/{id}/enrich        ask now instead of waiting
POST /api/captures/{id}/setlist       paste one by hand (works with no key)
POST /api/captures/{id}/confirm       accept a proposal that was not certain
POST /api/captures/{id}/decline       reject it; remembered, keeps looking
GET  /api/captures/unmatched          everything with no confirmed setlist
POST /api/captures/{id}/fix           corrections; reopens a settled lookup
GET  /api/setlistfm/status            api / manual / backoff
```

**Known inconsistency:** `_capture_live()` names files from `date.today()` (the
air date) while the NFO carries the festival-local performance date. For a
late-night set these differ by a day — `S2026E0418` on disk, `aired 2026-04-17`
in the metadata. Plex displays the NFO date, so it is cosmetic, but the two
disagree. Left alone deliberately: changing the naming convention mid-library is
what produced the `Season 2019` / `2023` / `2025` mess in `Band C/`.

## Rate limits are PINNED, in one place

`app/ratelimit.py` declares every external service's limits as data, with a
mandatory **`source`** field saying whether a number was *published by the
vendor* or *chosen by us*. `GET /api/ratelimits` shows them live with usage.

| service | our budget | **their published limit** | source |
|---|---|---|---|
| setlist.fm | 1s · 1,200/24h | **2.0/sec · 1,440/day** | `setlist.fm/settings/api` (login required) |
| MusicBrainz | 1.2s · 5,000/24h | **1 req/sec**, enforced with HTTP 503 | MusicBrainz API docs |
| YouTube | 2s · 4,000/24h | none published | `max-age=900` is the only stated figure |

Where a vendor publishes limits, our budget sits **strictly below** them, so we
trip our own ceiling first and never theirs. A self-imposed cap the vendor's can
exceed is not a margin.

**Why this file exists.** Throttling used to live beside each caller with the
limit stated in a comment, and two things went wrong that the arrangement made
likely:

1. **The throttle contradicted its own comment.** A 6-second interval permits
   10 requests/minute — 14,400/day — while the comment beside it called that "a
   fraction of" one per minute. Nobody proofreads arithmetic in a comment.
2. **An interval was mistaken for a budget.** Spacing is not a ceiling.
3. **A limit was declared unpublished after checking only the public docs.**
   setlist.fm's public documentation states no figures, so "publishes no rate
   limit" *looked* verified — and was wrong. The real limits (2/sec, 1,440/day)
   are on the API-key page, behind a login. **An unauthenticated check cannot
   establish that a limit is unpublished, only that it is not on the page you
   read.** When a vendor issues credentials, check the credential page.

Hence two fields: `source` (where the number came from) and `published` (what
the vendor states, as distinct from what we allow ourselves).

**Two mechanisms, both required.** `min_interval` stops bursts; `daily_cap`
stops runaways. An interval alone is not a budget — that was the actual bug.
The cap is a **rolling 24h window** (not calendar-day, which invites a burst at
midnight) and is **persisted in sqlite**, so restarting the container does not
hand the app a fresh allowance.

**An exhausted budget is not an error.** `acquire()` returns `(False, reason)`
and callers skip cleanly and retry later. `setlistfm.available()` reports mode
`budget_spent` with remaining counts. Treating a deliberately conservative
self-imposed limit as a failure would make correct behaviour look like a broken
integration.

MusicBrainz is listed but **not called directly** — `artist-lookup.py` reaches
it through Lidarr, which owns its own throttle. The entry exists so anything
added later inherits the right number instead of rediscovering it via 503s.
(`en.wikipedia.org` and `coconut.co` in `codec-compat.py` are citation URLs in a
static table, and the `w3.org`/`yahoo.com` URLs are XML namespaces — none are
runtime calls.)

## Static-image videos are audio bootlegs

`scripts/rip-concert-audio.py` — a still image with a full concert under it is
worthless as video and complete as audio. When the uploader typed a timestamped
setlist into the description, that is a hand-made track index, and this splits
and tags on it.

```bash
./scripts/rip-concert-audio.py URL                    # show the plan
./scripts/rip-concert-audio.py URL --apply --source-type AUD
```

**Do not rely on `yt-dlp --split-chapters`.** It silently finds nothing on most
hand-typed setlists. Measured on the Band B/Guest Artist Amphitheatre 1995 video: 16 clean
timestamps in the description, `chapters: 0`, because the lines are numbered
`1) 0:00 - Opening Song` and the leading `1)` defeats yt-dlp's parser. The
script checks yt-dlp's chapters first (free, and right when it works) and falls
back to its own parser, which is the common case.

**The date comes from the TITLE, never `upload_date`.** That video was performed
1995-10-21 and uploaded 2019-12-09 — a **24-year** gap. Using the upload date
files a 1995 show under 2019, which is precisely how `Band C/Season 2019` ended up
holding 1992 concerts. If no date can be parsed, the script refuses and asks.

**Lossy stays lossy.** YouTube serves Opus/AAC; transcoding to FLAC inflates it
5–10× and recovers nothing. Tracks are cut with a **stream copy** — verified
byte-for-byte equal at 73 MB in and 73 MB out, and the 16 split durations sum to
78:53 against a 78:53 source, so no drift. `--flac` exists but is a lossless
container around lossy audio.

**Guests are per-track, not per-album.** The title said "Band B with
Guest Artist"; Guest Artist joined for the last five songs. The first implementation
fell back to the album-level guest for every track and so tagged Guest Artist onto
"Opening Song", which he did not play on — a confident, silent falsehood that
nothing downstream re-checks. `GUEST` is now per-track only; the show-level
billing lives in `ALBUMGUEST`.

**The NFO is documentation, not matching data.** Plex **music** libraries read
embedded tags and ignore NFO entirely — `album.nfo` is written because it is the
only human-readable record of provenance that survives outside the database.
This server has no NFO-capable music agent (`Personal Media Artists`, `Last.fm`,
`Plex Music`, `Plex Personal Media`), so editing it changes nothing in Plex.

### Guest artists: the credit stays honest, the playlist makes it findable

Measured 2026-08-21 by A/B test on FRESH album folders (fresh because Plex will
not re-read tags on content it has already ingested — see below), with
`Prefer local metadata` ON:

| track `ARTIST` | album intact | searchable as the guest |
|---|---|---|
| `Guest Artist` (bare) | ✓ | **✓** |
| `Band B with Guest Artist` | ✓ | ✗ |
| `Band B/Guest Artist` | ✓ | ✗ |
| `Band B;Guest Artist` | ✓ | ✗ |
| `ALBUMARTIST = Guest Artist` | ✓ but files the WHOLE show under Guest Artist | ✓ |

**Plex indexes a track credit only when it is a bare artist name it can
resolve.** No delimiter changes that, and album **collections are not searchable
either** — the tag applies and returns nothing.

So there is a genuine trade-off and the resolution is deliberate: the per-track
credit stays EXACT, because the tag is the permanent record that travels with
the file, and findability is added alongside as a **playlist**, whose title is
searchable. Writing a bare `Guest Artist` onto five Band B songs would
buy search by making the file lie. `rip-concert-audio.py` creates that playlist
automatically whenever a rip has guests (`--no-playlist` to skip).

### The "featuring" convention — `scripts/tag-featuring.py`

Scans audio libraries for track credits like `X with Y` / `X ft. Y` /
`X featuring Y`, then does two things that solve two DIFFERENT problems:

| | all accounts | searchable | browsable | credit stays honest |
|---|---|---|---|---|
| album-title suffix `(with X)` | ✓ | **✓** | ✓ | ✓ |
| collection `Featuring X` | ✓ | ✗ | **✓** | ✓ |
| playlist | probably not — per-account | ✓ | ✓ | ✓ |
| bare `ARTIST` tag | ✓ | ✓ | ✓ | **✗** |

```bash
./scripts/tag-featuring.py                       # dry run, all audio libraries
./scripts/tag-featuring.py --section 9 --apply   # Live Music
./scripts/tag-featuring.py --apply --no-title    # collections only, non-invasive
```

**What spans libraries — measured, not assumed**

| | spans libraries | searchable | all accounts |
|---|---|---|---|
| **Playlist** | **✓** (HTTP 200, items from 2 sections) | ✓ | **NO — per-account, CONFIRMED** |
| Collection | ✗ (**HTTP 400**, membership unchanged) | ✗ | ✓ |
| Album-title suffix | n/a — per album | ✓ | ✓ |

Collections are hard-scoped to one section, so a `Featuring Guest Artist`
collection can **never** cover both the studio Music library and Live Music.
Playlists can — but **playlists are per-account, confirmed by checking a second
account on this server**: that user saw the album and the collection, and did
**not** see the playlist.

So the ranking is settled. **The album-title suffix is the mechanism**: global,
searchable, and per-album so it never needed to span libraries. Collections give
every account a browse category (one library each). Playlists are a private view
for whoever ran the script.

**Two Plex traps found repairing this:**
- **Renaming a folder mints NEW ratingKeys**, silently emptying any collection
  or playlist that referenced the old ones — that is what produced a 0-item
  collection and a 0-item playlist here. **Always build collections and
  playlists AFTER any renaming, never before.**
- **Playlist creation URI:** one URI with **comma-joined ratingKeys**. Comma-
  joining whole URIs returns **HTTP 200 and creates an EMPTY playlist**:
  `single uri -> 1 item`, `comma-joined uris -> 0`, `comma-joined keys -> all`.

**A named bootleg keeps its name.** Titles like `Artist - The Oddball Couple
(Live at Venue DATE)` carry the name the recording circulates under, and filing
it as a bare date throws away what people would search for. `parse_title()`
extracts it, rejecting non-names like `Live at…` and `Full Concert`. Unnamed
shows still fall back to `YYYY-MM-DD - Venue`; the date lives in the `DATE` tag
either way.

`--playlists` adds individual TRACKS, not albums: on a studio record only one
track has the guest, and adding the album would put eleven songs they had
nothing to do with into their playlist.

**Scale it with `--min-tracks`.** On this library, 120 guests were found and
**105 appear on exactly one track**, so the default would bury the useful
playlists under a hundred single-item ones:

| `--min-tracks` | playlists |
|---|---|
| 1 (default) | 120 |
| 2 | 17 |
| 3 | 6 |
| 5 | 5 |

Whatever the threshold drops is **reported**, because a silent cap reads as
"covered everything" when it did not.

**Source type belongs in the tag, not the album title.** `[AUD]` means something
to a taper comparing sources and nothing at all when browsing for a show, so it
lives in `SOURCE_TYPE` and is appended to the album name **only** to break a
real collision — a second source of the same artist/date/venue, which collectors
keep deliberately and which would otherwise overwrite the first.

**Apply the title suffix to LIVE libraries only.** A dry run over the whole
server found **44 albums**, nearly all studio records where a "feature" is a
normal credit — renaming `Example Studio Album` to
`Example Studio Album (with Guest 1, Guest 2, Guest 3, Guest 4)` is
noise, and it is visible to everyone. `--no-title` gives the browse categories
without touching any title.

**It edits Plex, not the files.** Reversible, and it sidesteps the fact that
Plex re-reads edited tags unreliably. Fields are set **locked** so a rescan
cannot revert them.

Three traps, all hit here:
- **`PUT /library/metadata/{rk}`** is the endpoint that works. The section form
  `/library/sections/{k}/all?type=9&id=…` returns a bare **HTTP 400** with no
  explanation.
- **`POST /library/collections` with the item `uri` populates the collection.**
  PUT-ing a collection *tag* onto an album creates an **empty** one — that is
  what left a zero-item "Guest Artist" collection on this server, and it also
  invalidated an earlier "collections are not searchable" test that had been run
  against an empty collection. (Re-tested populated: still not searchable.)
- **Collections are not idempotent by name.** A second run POSTs a second
  collection with the same title and Plex keeps both. `collections_in()` and
  `collection_members()` check first; verified stable over three runs.

### Required Live Music library settings

Set on section 9 on 2026-08-21. Two of these are not optional:

| setting | value | why |
|---|---|---|
| Agent | `tv.plex.agents.music` | was `com.plexapp.agents.none` |
| **Prefer local metadata** (`respectTags`) | **on** | **while off, Plex prefers online matching and will not honour edited tags at all** |
| Genres | Embedded Tags | bootlegs have no online genre |
| Album Art | Local Files Only | use the ripped `cover.jpg` |

**`respectTags` being off is a trap.** It presents as "Plex refuses to re-read
my tags": a section refresh, `force=1`, a per-item refresh and a partial Plex
Dance all appeared to do nothing, which made several tag experiments look like
negative results when they were simply never ingested. Turning it on fixed the
re-read.

**Retagging content Plex has already ingested is unreliable even so.** New
folders ingest correctly every time. When testing tag behaviour, always use a
folder Plex has never seen rather than editing one in place.

## Docker discipline — three mistakes made here, do not repeat them

**1. Never `docker cp` code into a running container.** It was used repeatedly
during development and it silently forks the container from its image: every
patched file had a different md5 than the image, and one script did not exist in
the image at all. Everything "verified" that way was verified against a
hand-patched container, not against what a fresh deploy produces. The fix is
`docker compose up -d --build --force-recreate`, then confirm:

```bash
IMG=$(docker inspect encoreum --format '{{.Image}}')
docker exec encoreum md5sum /app/app/captures.py
docker run --rm --entrypoint sh "$IMG" -c 'md5sum /app/app/captures.py'   # must match
```

**2. `docker exec` runs as ROOT by default.** This container drops to PUID via
`gosu`, so every `docker exec` that writes a file leaves it root-owned in a
volume the app can only touch as uid 1000 — which surfaces later as a
`PermissionError` from the app, far from the cause. Use
`docker exec -u 1000:1000` for anything that writes.

**3. `group_add` does NOT survive `gosu`.** This one is genuinely hard to see:
Compose's `group_add` adds supplementary groups to the container's *init*
process, but `gosu` re-initialises the group list from `/etc/group` when it
drops privileges, so the injected gid is dropped. The symptom is that
`docker exec -u 1000` can read a secret while the app reports permission denied.

`entrypoint.sh` now handles it: for each file in `/run/secrets`, it takes the
gid the HOST file is owned by, ensures a group with that gid exists in the
container, and adds `appusr` to it before dropping privileges.

### Secrets — see `docs/docker-secrets-notes.md`

**Every credential is a docker secret. None is an environment variable.**
`PLEX_TOKEN`, `LIDARR_API_KEY`, `SETLISTFM_API_KEY` all come from
`/run/secrets/` via `app/secrets.py` (`$NAME_FILE` → `/run/secrets/<name>` →
`$NAME`, the postgres/mysql/nextcloud convention). `GET /api/credentials`
reports the source of each and flags env-var sourcing as `secure: false`; it
never returns a value.

This was applied to **one** credential at first and documented as though it were
the rule — while the Plex token, the one that had already leaked, stayed a plain
env var. Half-applied discipline is worse than none, because the docs then claim
a property the system does not have.

`root:encoreum-secret 0640` on the host, granted to exactly this container —
the same shape as `smtp_password` in the the host stack: **a dedicated system group
per secret**. Under plain Compose a secret keeps the host file's ownership and
mode; the `uid`/`gid`/`mode` keys in a secret definition are **Swarm-only and
silently ignored**.

Owning the file `1000:1000 0600` also works and was tried first — reject it. It
leans on the secrets *directory* being untraversable rather than on the file's
own permissions, so it fails open the moment those directory permissions change,
and it puts the key under the operator's own account. The group approach keeps
the key unreadable by `your-user` regardless.

The app reads `/run/secrets/setlistfm_api_key` and falls back to the env var;
`key_source()` reports which, without printing the key, because "unreadable
secret" and "no key configured" are otherwise indistinguishable.

## Conventions

- **Commit style**: bodies explain fully. `Co-Authored-By` only on a repo's
  first commit.
- **Never commit** `config/` (jobs.db, logs, archives, venv) or `.env` — both are
  gitignored. Ship `.env.example` instead.
- **Validate before shipping**: `python3 -c "import yaml;yaml.safe_load(open('docker-compose.yml'))"`
  and rebuild with `docker compose up -d --build` from the repo dir.
- The app is **not** part of the Deployrr stack in `~/docker/compose/the host/`.
  It has its own `docker-compose.yml` and is managed from this directory.
- `main.py` keeps a **denylist** of yt-dlp options (`_DENIED_OPTS`) for
  user-supplied extras. Extend it rather than removing the check. Options that
  are safe but wrong for this library go in `_REFUSED_OPTS` instead, with a
  reason shown to the user (first entry: `--split-chapters` — chapter MARKS are
  already embedded via `--embed-chapters`, which is what Plex reads).
- Every child process runs with `cwd` = a per-job scratch dir
  (`STAGING_DIR/<job_id>.cwd`, via `JobControl.cwd`), never the container's
  root-owned `/app`, so output yt-dlp writes outside our `-o` template can't
  fail with Permission denied.

## Content type: Auto/Manual + Video/Audio toggle (added 2026-09-26)

Every preset-detection feature above (suggestion chips, the preset nudge) is
the server inferring content type AFTER looking at the playlist. A real
objection: **the person entering the URL already knows what they're
downloading** — there should be a way to tell the app that BEFORE Load URL,
not just react to guesses after.

- **Auto** (default) = today's behavior unchanged: content type stays locked
  until Load URL, then can be adjusted/nudged.
- **Manual** unlocks the Video/Audio toggle + the `#naming` sub-type select
  IMMEDIATELY, before Load URL — `applyTypeLock()`'s rule: `locked =
  !formUnlocked && !manual`. Once `formUnlocked` (Load URL clicked), content
  type is always editable regardless of Auto/Manual; the toggle only matters
  for the PRE-load window.
- **Video/Audio is a real toggle now** (`.av-btn[data-av]`), not just the
  `<optgroup>` split inside one dropdown. Clicking one calls `setAV(av)`,
  which repopulates `#naming` from `PRESET_OPTIONS` (the same `video_presets`/
  `audio_presets` data the server used to build the original `<optgroup>`s,
  passed to JS via `|tojson` so the two can't drift) and dispatches synthetic
  `input`+`change` events — mimicking a real `<select>` pick exactly, so it
  reuses the existing invalidate-stale-preview hook (`input`) and
  `updateNamingUI()` (`change`) instead of duplicating their logic.
- **Solves the Author/Title mixup a different way than the chip-routing
  fix above**: if you pick Manual -> Audio -> Audiobook BEFORE Load URL,
  `preview.py` gets `naming=audiobook` on the very FIRST fetch, so
  `_show_suggestions()`'s channel-vs-title split happens correctly from the
  start — no after-the-fact chip rerouting needed. The chip-routing override
  still exists for Auto mode / getting it wrong.
- **Deferred** (explicit user decision 2026-09-26): "TV episodes / Full movie
  / Concerts / Music video" as Video sub-types was the original ask, but
  those aren't four things this app does differently today — the 5 existing
  video modes are all *concert* organization schemes (different dating/
  numbering for the same content type), and Full Movie / Music Video would
  need genuinely new pipelines (a movie needs a DIFFERENT Kodi NFO schema —
  `<movie>` root, not `<episodedetails>` — and a music video needs Plex's own
  `Artist/Artist - Title.ext` convention, neither of which `generate-nfo.py`/
  `generate-plex-art.py` do today). Decided to ship the toggle now with the 5
  real video modes rather than add labels with no distinct behavior behind
  them; Movie/Music-Video pipelines are a separate future project.

## Genres/Studio don't default for audio downloads (added 2026-09-26)

Both fields only ever reach `generate-nfo.py` (`pipeline.py`'s `--genre`/
`--studio` flags), which is never called at all for audio — same reason
NFO/artwork are skipped (see "Audio only" checkbox and the audio presets
above). A real complaint: the Genres field defaulting to `"Music, Concert"`
for an **audiobook** is actively wrong, not just inert — showing it dimmed
wasn't enough, the visible default itself was misleading.
`applyAudioOnlyDimming()` now also disables+dims both fields for the same
`noVideoMeta` condition, and clears/restores Genres — but ONLY the sentinel
default/empty state (`DEFAULT_GENRES = 'Music, Concert'`), never a value
someone actually typed, so switching modes back and forth never clobbers a
deliberate edit.

## Filename-only shortening for long audio titles (added 2026-09-26)

A real complaint: `{Title} - 001 - {Chapter Title}.ext` gets unwieldy for a
long book/show title combined with a long chapter title (a real example:
"New Tales of the Example Mythos" + "A Chapter Title That Keeps Going Well Past Any Sensible Length"). Two DIFFERENT fixes for two different parts,
deliberately not the same mechanism:

- **The Title prefix** (`pipeline.short_title()`, `SHOW_ACRONYM_THRESHOLD =
  40` chars) — acronym-ized, e.g. "New Tales of the Example Mythos and Other
  Strange Stories" -> `NTEMOSS` (skips short stopwords: a/an/and/as/at/
  but/by/for/from/in/of/on/or/the/to/with, so it reads closer to real
  initials). This is the right tool here specifically BECAUSE the full title
  is already the folder name (`Author/Title/` or `dest/Show/`) — repeating it
  in full on every track costs nothing to skip, the real name is one level
  up. Below the threshold, titles pass through untouched — most audiobook
  titles never trigger this at all.
- **The per-track chapter/episode title** — truncated, NOT acronym-ized. An
  acronym of a full sentence-like title ("A Chapter Title That Keeps Going...")
  would be unreadable; truncation preserves far more signal per character for
  something that's supposed to be recognized at a glance. Implemented as
  yt-dlp's OWN `%(title).<N>s` precision syntax (`CHAPTER_TITLE_MAX = 80`) —
  verified this truncates exactly like Python's `%.Ns` string formatting
  (plain slice, not word-boundary-aware) — rather than custom Python, because
  this field isn't known until yt-dlp resolves each video at download time,
  unlike `show` which is already a plain string before the command is built.
- **Neither touches the folder name or embedded metadata.** `--embed-metadata`
  already writes the real full title into the file's tags; Audiobookshelf
  reads those, never the filename. Shortening the on-disk name costs nothing
  anywhere a person actually sees the title.
- **`preview.py` mirrors both** (imports `pipeline.short_title`/
  `CHAPTER_TITLE_MAX` directly rather than reimplementing) so the dry run
  shows exactly what a real download will produce — this is the same
  drift-prevention lesson as the writeHint/`/api/show-info` fix above: two
  independent implementations of one naming rule is how that bug happened.

## Clip range: trimming outros was BROKEN, not missing (fixed 2026-09-26)

Asked "is there a way to trim ending outros/promotion" — there already was a
**Clip range** field whose own placeholder advertised `-30` for exactly this,
but `main._parse_clip()` got it backwards: `"-30".partition("-")` splits on
the leading `-` itself (there's nothing before it), producing `start="",
end="30"` -> `*0-30` — **keeps only the FIRST 30 seconds**, the opposite of
"trim the last 30". The placeholder had been promising a feature that did the
reverse of what it said.

yt-dlp itself already does the right thing — confirmed directly:
`--download-sections "*0--5"` on a real 19-second video produced exactly a
14-second file (`Duration: 00:00:19.02` in, `duration=14.000000` out).
"Negative timestamps are calculated from the end" is yt-dlp's own documented
behavior (`--help`), not something built here — the bug was purely in how
this app's `_parse_clip()` decided where start ends and end begins.

**The fix**: stop taking the first `-` unconditionally.
- If the whole value starts with `-` (e.g. `-30`), it's a lone negative END
  offset — start defaults to `0`, end is the value as-is (`*0--30`).
- Otherwise, split on the first `-` found AFTER position 0, so a negative
  END's own sign in a compound range (`10--30` = from 10s to 30s-before-the-
  end) is never mistaken for the separator.
- `_TIME_RE` had to allow a leading `-` too (`^-?[\d:.]+$`) — it was
  rejecting valid negative end times outright before parsing ever got a
  chance to matter.

Verified against a real download (not just the parser in isolation): a job
with `clip=-5` on the 19-second "Me at the zoo" test video produced a real
14-second `.opus` file. `24` alone (skip an intro) and `1:30-5:00` (an
explicit range) were already correct and are unchanged. Applies to every
naming mode via the shared `_common_opts()` — includes audiobook/podcast/
music, the actual case this was asked about.

## Error troubleshooting + retry (added 2026-09-29)

Triggered by a real 132-item audiobook job that failed **every single item**
with `HTTP Error 403: Forbidden` on the actual audio data (metadata/thumbnail
succeeded first every time) — and the app gave zero indication of why, or
even that it was a 100% failure rather than a handful of stragglers. Three
separate things were wrong, found in this order:

**1. Root cause — YouTube now gates audio-only formats behind a PO Token.**
`-f bestaudio/best` LOOKS like it succeeds (yt-dlp picks format `251`, Opus),
but the actual data URL 403s at download time because the default client
lacks a PO Token — confirmed via `--verbose`: `PO Token Providers: none` +
"YouTube is forcing SABR streaming for this client... web client https
formats have been skipped as they are missing a URL." Since the failure
happens AFTER format selection, not during it, `bestaudio/best`'s own
fallback chain never gets a chance to help. Tested every plausible
`--extractor-args youtube:player_client=...` against a real failing video:
`android` and `android_vr` both skip the gated format entirely and fall back
to a combined video+audio format (itag 18) that isn't PO-Token-gated —
`android` was picked (android_vr explicitly warns it still wants a token for
some of its own formats). **Fix**: `_common_opts()`'s audio_only branch now
passes `--extractor-args youtube:player_client=android`. Trade-off: costs
bandwidth (the video track downloads and `-x` discards it) — but a working
lower-efficiency download beats a 100%-failure high-efficiency attempt.
Verified end-to-end twice: a direct test (real ~35-minute file, no 403) and
a real retry of the originally-failed job (item 1 succeeded immediately,
where it had 403'd every previous attempt). Scoped to `audio_only` only —
the video path has its own separately-tuned client/format resilience from
the 2026-07-09 throttling work that this must not disturb.

**2. `jobs._has_video` only recognized VIDEO extensions.** The function that
decides "partial" (some real content, despite errors) vs. "error" (nothing
real) checked `_VIDEO_EXTS` only — so ANY audio job with ANY error, even one
failure out of 500 successes, was indistinguishable in the UI from this
100%-failure case; both just said `error`. Renamed `_has_media`, added
`_AUDIO_EXTS`, checks `_MEDIA_EXTS = _VIDEO_EXTS | _AUDIO_EXTS`. This job
happened to still correctly show `error` (genuinely 0 real files), but that
was accidental — the check wasn't looking at audio at all.

**3. No way to see WHY, short of reading a 2000+ line raw log.** Added
`jobs.error_summary(job_id)`: parses `ERROR:` lines from the log, dedupes by
message, counts occurrences (132 identical errors reads very differently
than 1), and attaches a plain-language `hint` for a small set of recognized
patterns (403, private/unavailable video, age-restriction, copyright claim —
extend `_ERROR_HINTS` as new patterns turn up; deliberately not trying to
cover everything yt-dlp can say). Exposed via `/api/jobs/{id}` and rendered
on `job.html` in a new "What went wrong" card, visible whenever a job has
any error lines regardless of final status.

**Retry, added for the same reason**: `POST /jobs/{id}/retry` /
`jobs.retry_job()` starts a NEW job (original stays as the historical record)
with the ORIGINAL job's exact params. Deliberately does **not** try to
compute which playlist items failed — `--download-archive` already knows,
correctly, because yt-dlp only writes an entry there once a video FULLY
completes. A failed item was never archived, so resubmitting the same params
naturally retries only what failed and skips what already succeeded — "retry
the job" and "download only the failed items" are the same mechanism, for
free, with no separate log-parsing-based tracking to keep in sync or get
wrong. `job.html` shows "Retry (skips items already downloaded)" whenever a
job finished without full success (any TERMINAL status except `done`).

**Also fixed in passing**: `job.html`'s own client-side `TERMINAL` array was
missing `'partial'` (present in `jobs.py`'s), so a job that settled into
`partial` left the Cancel button clickable forever.

## Auto-chunking large playlists (added 2026-09-29)

Prompted by watching a real 132-item audiobook job: one 30-40 minute job with
no feedback in between felt stuck (it wasn't — see the "output should update"
investigation in this same session, real progress just arrives every ~15-30s
per item because of the deliberate anti-403 sleep intervals), and a failure
partway through means retrying the WHOLE playlist rather than the part that
actually failed.

**`main.py`'s `/jobs` handler** (`CHUNK_THRESHOLD = 30`, `CHUNK_SIZE = 20`):
if the effective item count (the user's own `include` selection, or a fresh
`preview.flat_count()` when none was given) exceeds the threshold, it's split
into slices of `CHUNK_SIZE` and handed to `jobs.create_chunked_jobs()`
instead of a single `create_job()`. Never applies to a live capture (a single
stream, not a playlist).

**`jobs.create_chunked_jobs()`**: creates one job row per chunk — same show/
destination/download-archive, different `include` slice — pre-generates all
the job ids so each chunk's params can carry `_chunk_next_id` (the next one to
start), `_chunk_group`/`_chunk_index`/`_chunk_total` (for labeling/linking).
Only the FIRST chunk is started immediately.

**Sequential, not parallel, deliberately.** `_advance_chunk()` (called from
`_run()`'s completion path, for every terminal status) starts `_chunk_next_id`
when the current chunk finishes. Running chunks in parallel would hit
YouTube harder than one big job ever did, undoing the rate-limit tuning
`_common_opts()` already has. `done`/`partial`/`error` all advance to the next
chunk (one chunk's trouble shouldn't block the rest — same `--ignore-errors`
philosophy as everything else in this app); `cancelled`/`interrupted` do NOT
— an explicit or ambiguous stop must never silently keep going.

**Retry interacts with chunking carefully.** `retry_job()` strips
`_chunk_next_id` from the retried job's params before creating the new job —
otherwise retrying one failed chunk would re-trigger starting the NEXT chunk
all over again, even though that chunk already ran (or is running) from the
original chain's own advance. The chunk group/index labels are kept, so the
retry still correctly shows "part 3 of 7".

**UI**: `job.html` shows "Part N of M" in the header plus a small nav strip
linking every sibling chunk (current one highlighted); the homepage jobs
table shows the same label inline. `jobs.list_jobs()` now selects `params`
too (previously only summary columns) and parses it in Python purely to
extract this label — no DB schema change, since chunk metadata lives entirely
inside the existing JSON `params` blob. `jobs.chunk_siblings(group_id)` finds
the other chunks via a `LIKE` pre-filter on that same params text (pragmatic
given modest job volume — no index needed) followed by an exact-match guard
in Python.

**Verified entirely offline before touching the live container** — a real
132-item job was running in production at the time, and rebuilding would
have interrupted it. Built a separately-tagged image (`encoreum:test`),
validated the full chunk lifecycle (sequential execution, chain continuing
past a deliberately-failed middle chunk, retry NOT double-triggering the
chain, correct labels/siblings) against an isolated temp SQLite DB with
`pipeline.run_job` mocked out, then separately confirmed both `index.html`
and `job.html` render the new markup correctly (including the chunk-nav with
the current part highlighted) via a real throwaway container hit over HTTP
— all before rebuilding the actual `encoreum` container.

**Two more things found deploying this to the actual failing job:**

1. **`retry_job()` bypassed chunking entirely** — it called `create_job()`
   directly, so retrying the original (never-chunked) 132-item job just re-ran
   it as one big job again instead of getting split. Fixed by extracting
   `jobs.start_new(params, show_dir)` — the one real entry point for "start a
   fresh top-level download," deciding chunk-or-not and doing the right thing
   either way — and having BOTH `main.py`'s `/jobs` handler and `retry_job()`
   call it, instead of duplicating (and silently diverging on) the threshold
   check. `CHUNK_THRESHOLD`/`CHUNK_SIZE` moved from `main.py` to `jobs.py`
   accordingly (chunking is a job-creation concern, not a request-handling
   one). Verified with a scripted test simulating exactly this: a large
   non-chunked "original" job's retry now comes out correctly split into 3
   chunks; a small (≤30 item) job via the same `start_new()` still stays a
   single job.

2. **A restart mid-job can silently lose completed work — a real, not
   hypothetical, data-integrity gap.** Discovered when the container was
   rebuilt while the real 132-item job was ~90% through: `init_db()`
   unconditionally wipes `STAGING_DIR` on every startup (by design, to clear
   a crashed job's leftovers), but a NON-chunked job doesn't `_publish()`
   ANYTHING until its single yt-dlp command finishes the WHOLE playlist — so
   119 already-fully-downloaded chapters were sitting in staging, never
   published, and the restart deleted all of them. Worse: `--download-archive`
   lives in `/config` (never wiped), so it still had all 119 entries recorded
   as done — meaning a naive retry would have SKIPPED those 119 (trusting the
   archive) even though zero of them actually existed anywhere anymore,
   silently producing a 13-chapter "book" with no error at all. Caught by
   checking actual file counts (archive line count vs. real `.m4a` files on
   disk) before retrying, not by trusting either signal alone. Fixed for that
   one job by deleting the stale archive file before retrying (forces a
   correct full redownload). **Not fixed at the architecture level** — chunking
   itself is a big practical mitigation going forward (a chunk publishes as
   soon as ITS OWN small batch finishes, so a mid-job restart loses at most
   one chunk's worth of progress instead of the whole playlist's), but the
   underlying "staging wiped unconditionally, archive isn't" mismatch still
   exists for a non-chunked job or a chunk that's still mid-run at restart
   time. Worth a real fix later: either have staging survive a restart, or
   validate archive entries against actual file existence before trusting
   them on retry.

## Browsing to an existing Author/Title + continuing a serial's numbering (added 2026-10-05)

Prompted by a real case: "Example Serial" (audiobook) got a new batch of
chapters on YouTube after the first ~50 were already downloaded. Two separate
problems, fixed together:

**1. The destination tree couldn't be used to pick a folder, and audiobook
folders looked empty in it anyway.** `library._counts()`/`browse()` only ever
counted `VIDEO_EXTS` — an audiobook/podcast/music folder's files are audio
extensions, so every audio destination showed `0 videos` in the tree and in
`/api/show-info`'s season list, regardless of how many chapters were actually
there. Centralized the extension sets in `presets.py` (`VIDEO_EXTS`,
`AUDIO_EXTS`, `MEDIA_EXTS` — previously three independently-drifting copies
across `library.py`, `jobs.py`, and a dead unused one in `pipeline.py`) and
switched the counting to `MEDIA_EXTS`. The JSON field is still called
`videos` (renaming it would've meant touching the API shape, the tree UI, and
`show-info`'s `seasons` list for no behavior change) — it now just means "real
media files," which is what every caller already treated it as.

Added an explicit **"Use" button** to both tree levels (`index.html`
`showRow()`/`useFolder()`) so Author/Show can be filled from a real existing
folder name instead of retyped (typo risk = an accidental near-duplicate
folder). In audiobook mode the row = Author, the leaf = Title (matching the
on-disk `dest/<Author>/<Title>/` layout); in every other mode the row = Show
and leaf-level Use is a no-op (a season subfolder isn't a Show). Required
splitting `.tree-row` from a single `<button>` into a `<div>` holding a
`.tree-toggle` button + a sibling `.tree-use` button — a `<button>` nested
inside a `<button>` is invalid HTML with unpredictable click/propagation
behavior, so the expand/collapse toggle moved to its own child button.

This is the SAME underlying gap as the already-documented fuzzy-match
limitation above (existing-folder suggestion chips only scan the top level of
a destination) — the tree's explicit browse-and-pick now covers the case the
fuzzy match still can't (an existing Title nested under an Author folder),
so that limitation is less urgent but still not fixed at the fuzzy-match
layer itself.

**2. Numbering would've restarted at 001 and collided with the existing 050
chapters.** Audio presets number by true playlist position
(`flat_audio_template`'s `%(playlist_index)03d`) — pointed at a NEW,
separate URL for the new batch, that always starts back at 001. Added
`pipeline.existing_max_seq(show_dir)` (scans for the highest `NNN` already in
the folder — the `" - NNN - "` pattern flanked by spaces/dashes on both sides
is a reliable anchor even though a Title can legitimately contain its own
`-`) and `pipeline.continue_sequence(published, existing_max, log)`, called
right after `_publish()` for audio-preset jobs. It only shifts when the new
batch's own lowest number would actually collide (`<= existing_max`) — a
re-run of the SAME growing playlist (where the archive skips already-done
IDs and `playlist_index` already reflects each item's true, already-correct
position) is left alone, since shifting it again would double-count.
Verified both cases offline (fresh batch restarting at 001 → renamed to
051-055; same-playlist continuation already at 051-055 → untouched) before
touching anything live.

Preview mirrors the exact same collision rule (`_preview_flat_audio` in
`preview.py`) so the numbers shown before confirming match what the real run
will produce, and adds a note when a shift is actually happening — same
"never let a preview silently diverge from the real run" rule as everything
else previewed in this app.

**Scoped to staged (non-`direct`) mode only.** Direct mode writes straight
into the library with no staging/publish step, so there's no `published`
list to tell "new this job" from "was already there" — the distinction
`continue_sequence` needs. Not fixed here; a serial's new batch should go
through the normal (default) staged path, not "work directly in library".

## Page audio capture (added 2026-10-31)

`scripts/capture-page-audio.py` saves what a web page's player streams, for sites
yt-dlp can't read. A `page_capture` param on any audio preset; only the source of
the bytes changes, so staging, publish, numbering and `continue_sequence` are the
normal path. User docs are in the README. Tests: `tests/test_page_capture.py`.

**Boundaries, deliberately not options:** no DRM (EME request or encrypted manifest
= refusal), no anti-bot evasion (no stealth, no spoofed identity, no CAPTCHA
handling), no entering or bypassing someone's credentials or PIN. A person logs in
themselves through `scripts/page-login.sh`. One page per job: no site-specific
chapter harvesting or crawling. Extend it for delivery mechanisms, not around these.

Things that look wrong or were found the hard way:
- **Playwright disables the sandbox by default.** `launch()` passes `--no-sandbox`
  unless `chromium_sandbox=True`, so a passing test proved nothing until that was
  explicit. With it on, Chromium can't start on the host (AppArmor userns restriction
  plus Docker's default profile), so capture needs `CHROMIUM_NO_SANDBOX=1`, with
  the exposure documented in the README. Real fix if this ever faces untrusted
  sites: run the browser in its own container with no library or secrets mounted,
  and attach over `--cdp`.
- **`init: true` is required.** uvicorn as PID 1 doesn't reap, so Chromium helpers
  became zombies (29 after a few jobs). Measured 0 with init, and 0 live browser
  processes after a cancel.
- **`storage_state()` over CDP misses local storage** for tabs Playwright didn't
  open, which is every tab in the login flow. `save_session()` reads it from the
  tabs directly. Plex Web keeps its login there, so without this the session saves
  and then fails later.
- **Re-fetch rather than record.** A player's own requests are partial 206 ranges.
  The whole file comes from a fresh GET with the page's cookies and Referer, with
  cookies dropped on a cross-host redirect.
- **The archive is written after publish, by the app, not by the script.** Same
  file as yt-dlp's archive (`pagecapture <key>` lines, which yt-dlp ignores). Keys
  are host + path, since signed URLs change on every load.
- **`ffmpeg` fetches outside the browser's guard,** so `manifest_problem()` walks
  HLS/DASH manifests for encryption and blocked hosts before handing them over.
- **Every result goes through `ffprobe`,** or a site's HTML error page gets
  published as a chapter. Tests cover this.
- **`.plex.direct` allowances apply only when the page itself is listed.** Otherwise
  any public page could use `192-168-0-1.x.plex.direct` to reach the LAN.

Plex Web: a token alone stops at Select User, and a Home owner's profile has a PIN.
That's why the session flow exists. Loading Plex Web also talks to every server
shared with the account and to plex.tv analytics, like any browser session.

**Not verified:** the Plex Web player itself. `PLAY_SELECTORS` includes
`[data-testid="preplay-play"]` as an unconfirmed guess until a real session runs.

## Chapters, sponsor marks, and Plex skip buttons (2026-11-16)

**Chapter MARKS, never split files.** `--embed-chapters` is always on and is
what Plex reads. `--split-chapters` is refused (`_REFUSED_OPTS`): it cut each
show into per-song files with no output dir, which failed Permission denied in
`/app` and would otherwise flood a Season folder with bogus episodes.

**"Mark sponsor segments as chapters" checkbox** (`sponsor_marks`, off by
default) adds `--sponsorblock-mark sponsor,selfpromo`. Not a default: it's an
outside lookup on every video and adds chapters among the song list. Measured
on real videos: an ad inside a chapter splits it (`X → [SponsorBlock]: Sponsor
→ X`); an ad ending on a boundary just shortens the chapter; a chapterless
video becomes `title → Sponsor → title`. Only sponsor/selfpromo, because
`intro`/`outro`/`filler`/`music_offtopic` get applied to walk-ons, banter and
encore breaks. `--sponsorblock-remove` stays allowed via Advanced but its help
warns against those categories.

**Sponsor scan** (`app/sponsorblock.py`, `/sponsors` page, admin only):
- *Preview*: after a preview renders, the page calls `/api/sponsors/check`
  separately (so SponsorBlock being slow/down never delays Preview), adds an
  "ad m:ss–m:ss" pill per item and offers to tick the checkbox. Read-only.
- *Library*: scans a destination using each file's `.info.json` id, lists
  hits, and **Add marks** rewrites only the chapter list (ffmpeg stream copy
  built in STAGING_DIR, length + chapter count verified, then `os.replace`).
  Original chapters go to `/config/chapter-backups/` first; **Undo** restores
  them exactly. Never cuts. `merge_chapters()` reproduces yt-dlp's
  `--sponsorblock-mark` output (tested against the three measured cases), so
  files marked either way look the same. Asks Plex to rescan the folder after.
- External service, opt-in, non-fatal: hash-prefix lookups (4 hex chars of
  sha256(id)), 6h cache, `ratelimit` entry `sponsorblock` (self-imposed; none
  published for GETs). Data is CC BY-NC-SA 4.0 — credit shown on both pages.
- First real scan, 2026-11-16: Concerts = 234 videos, **0** with segments.

**No file metadata makes a Plex Skip button.** Skip markers live in Plex's
database only, created by its own detection (intro / credits / ads, PMS
1.43.5). On the Concerts library (230 eps): 17 credits markers, 0 intro, 0 ad.
The documented `POST /library/metadata/{id}/marker` looks like per-user
bookmarks (its DELETE 400s "if the marker is not a bookmark"), not skip
markers; community tools that make Skip buttons write Plex's SQLite DB directly
— unsupported, not done here. Only 9 of 230 episodes have chapters, and all 9
match Plex exactly: the other 221 sources simply had none.

## Source credits, mode-mixing guard, add-ons (2026-11-16)

**Every NFO credits its source channel.** `generate-nfo.py` appends a
`Video source: <channel> on <site> — <channel url> · <video url>` line to the
episode plot (the plot is what Plex shows; it ignores `<source>`), adds a
`Source: <channel>` tag, and tvshow.nfo lists all source channels. Existing
NFOs are skipped by design, so `--add-credits` patches ONLY plot/tags in place
(never `--overwrite`, which would wipe setlist credits); the pipeline passes it
on every job. Backfilled across Concerts on 2026-11-16: 243 NFOs, 9 shows.
Concerts uses `tv.plex.agents.nfo.series`, so it displays.

**Never reorganise a populated show into another mode.** A performance-mode
Band A job moved all 224 existing videos from `Season YYYY/` into year
folders (name-by-performance runs on the WHOLE show). Reverted from the job
log's old→new pairs (896 files incl. sidecars) before Plex rescanned, and 25
NFOs numbered for the wrong layout were regenerated. Now
`pipeline.existing_layout()` snapshots the layout before download and
`layout_conflict()` skips the performance/sequence/rename step when it would
convert the show; Preview shows the same warning. Converting on purpose =
run the script by hand (dry-run first).

**Audiobook and Podcast are add-ons, off by default** (`presets.ADDON_NAMES`,
`settings.json` `"addons"`). Off = hidden from the Naming list and the
content-type nudge, and `/jobs` + `/preview` return a 400 saying how to turn
it on. Retries (`jobs.retry_job`) bypass the check on purpose so old jobs
still work. The core is live music; the `music` preset is NOT an add-on. On
the host, Audiobook was switched on (19 jobs used it); Podcast left off (1 test).

## Season titles "Season 2026" -> "2026" (2026-11-16)

Plex ignores `<title>` in season.nfo (ours already says `2026`) and folder
names don't matter (Band C's bare `2023/` folder still showed "Season 2023").
What sticks: `PUT /library/metadata/{season}` with `title.value=2026` +
`title.locked=1` — verified on PMS 1.43.5 to survive a forced refresh, same
ratingKey (nothing re-added). `plexlib.retitle_year_seasons()` runs every 30
min (APScheduler `plex-season-titles`) over ONLY shows Encoreum's video jobs
wrote to (`jobs.managed_show_dirs()`), ONLY default `Season <1900-2100>`
titles, never locked ones; `dry_run=True` previews. First run: 15 seasons in
9 Concerts shows; TV Shows untouched. Off: `PLEX_YEAR_SEASON_TITLES=0`.
Undo per season: unlock the title in Plex Web and refresh.

## Streams page: a channel's live streams on a timeline (2026-11-16)

`app/streamscan.py` + `templates/streams.html`, `/streams` (browse to view,
request rights to scan because it spends the shared YouTube budget).
Read-only: Record/Schedule/Download open `/?url=…[&live=1]`, the same prefill
the bookmarklet uses, so Preview and naming apply and a scheduled one parks
via the existing `is_upcoming` wait.

Measured facts it is built on:
- **A Live tab (`/streams`) flat listing** returns hundreds of entries in
  one call with id/title/thumbnail/duration/views/live_status but **no
  dates** (NASA: 400 entries, 2 is_live + 398 was_live, 0 dated).
- **Some channels have no Live tab** (Austin City Limits: "This channel does
  not have a streams tab"). Fallback checks the newest 50 uploads one by one
  (~170 s) and keeps is_live/is_upcoming/was_live; on ACL all 50 were
  `not_live` (premiered "Full Episodes" are not streams), so 0 is correct.
- Dates: RSS gives the newest ~15 free; per-video `--print` lookups (each an
  InnerTube call through `ratelimit.acquire("youtube")` — its first real
  user) fill upcoming start times and the 25 newest undated streams; "Scan
  more" does 50 more. Undated ones are placed by Live-tab ORDER between
  dated neighbours (`place()`), flagged `approx` and drawn dashed with "≈".
  Bug found by screenshot: items newer than the newest dated one were
  clamped to now−1h and piled up; they are now spread evenly up to now.
- Results cached per channel in `/config/streamscan/<UC…>.json`.

UI lessons from real-browser screenshots: the NOW line sat on top of the
live cards (they're placed AT now) and swallowed clicks → `pointer-events:
none` + behind cards; lanes are capped at 4 rows of cards and the rest
become dots above the axis (else 400 streams = a page-tall column); live,
upcoming and newest streams claim card slots first; the first jump to NOW
must be instant (a smooth scroll across years-wide track crawls).

## Public repo = a scrubbed snapshot (2026-11-16)

Two repos on purpose. **This one** (`onttm/encoreum`) keeps the full
history, whose bodies carry the reasoning and also name this deployment's
domain, hosts and paths. **The public one** (`onttm/encoreum`, AGPL-3.0) gets the
current tree only, made by `tools/publish-public.py` (private-only file):

```bash
tools/publish-public.py            # exports HEAD, scrubs, LEAK-CHECKS, syncs ../encoreum-public
cd ../encoreum-public && git diff  # review, then commit + push there by hand
```

The script refuses to publish if any FORBIDDEN pattern survives (domain,
host/Plex server names, home path, LAN IPs other than the 192.168.0.10
example). Add a pattern there whenever something new must never go public.
Host-specific values belong in `.env` (gitignored), not in tracked files:
`SECRETS_DIR`, `SECRET_GID`, `LIBRARY_NAME` moved there for this reason.
Pre-publication audit of all 33 commits: no credentials, every commit uses
the GitHub no-reply address, no sensitive file ever committed.

## Quick reference

```bash
cd ~/repos/encoreum
docker compose up -d --build          # rebuild + restart
docker logs -f encoreum             # follow
curl -s localhost:8681/api/history    # job history
sqlite3 config/jobs.db '.tables'      # job store
```
