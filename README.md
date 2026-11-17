# Encoreum

A small self-hosted **web app for yt-dlp** that downloads YouTube/Vimeo concert
playlists straight into your **Plex "Concerts as TV" library** — best quality,
date-coded episode names, thumbnails, and full NFO metadata — in one click.

It runs the whole pipeline for you:

```
download (yt-dlp)  →  rename by real concert date  →  build Plex/Kodi NFO
```

It is the Dockerised version of the three scripts in the *Plex Concerts as TV*
guide. Those scripts still live in `scripts/` and are called, unchanged, by the app.

---

## Optional integrations (Lidarr, Plex)

For festival schedules the app can say *"My Plex has 5 albums, no live
footage"* against each artist. That needs two credentials, both optional —
without them everything else works exactly as before and only the artist
extras go quiet.

```bash
cp .env.example .env && chmod 600 .env
# fill in LIDARR_API_KEY and PLEX_TOKEN, then:
docker compose up -d
```

Then **check it actually worked**, which is the part that usually gets skipped:

```bash
curl localhost:8681/api/queue/integrations
```

It distinguishes the three cases that need different fixes:

| Reported | Means |
|---|---|
| `not configured` | the variable is empty — set it in `.env` |
| `HTTP 401` | wrong key/token, or a Plex token invalidated by signing out everywhere |
| `unreachable` | wrong host — from a container the host is `172.17.0.1`, **not** `localhost` |

Credentials live in `.env` rather than a settings screen on purpose: they are
secrets, and secrets do not belong in an app database. Setup is by file; the
endpoint above is how you verify it.

## What you get

- A simple web page: paste a playlist URL, pick options, click **Start**.
- **Live log** of every download, streamed to your browser.
- **Job history** that survives restarts; re-running a playlist **skips** what you
  already have (resume-safe download archive per show).
- **Self-updating yt-dlp** — on every start, once a day, and via an **Update now**
  button. (YouTube changes often; this keeps downloads working.)
- Files land in your Plex library owned by the **right user**, so Plex can read them.
- **Complete Plex metadata, no plugin:** writes Kodi/XBMC `.nfo` (read by Plex's native
  `Plex NFO Series` agent on PMS 1.43.1+) **and** Plex-native posters/thumbnails.
- **Credits the source.** Every episode's description in Plex ends with the channel
  and site it came from, with links, and each show lists all of its source channels.
- **Won't scramble a show.** If a show is already organised one way, a job in a
  different naming mode adds its new files in the existing layout instead of
  moving every episode (Preview warns you first).
- **Seasons show as years.** Plex labels every season "Season 2026" no matter
  what the metadata says; Encoreum retitles the seasons of shows it downloaded
  to just "2026" (locked, so a refresh keeps it). Other libraries are never
  touched. Turn it off with `PLEX_YEAR_SEASON_TITLES=0`.
- A safety guardrail: the app will only ever write inside the folder you give it.

## Add-ons

The core is live music: concerts, live streams, setlists and taper uploads, plus the
*Music library (live audio)* preset. Two extras use the same download engine for other
kinds of audio. They're **off until you switch them on** with **Add-ons** (top right):

| Add-on | What it does |
|---|---|
| **Audiobook** | Audio only, into `audiobooks/<Author>/<Title>/`, one file per chapter, ready for Audiobookshelf. |
| **Podcast** | Audio only, one flat folder per show. |

Switching an add-on off only hides it from new downloads. Folders it already made,
past jobs and *Retry* keep working. The setting lives in `/config/settings.json`
(`"addons": ["audiobook"]`).

---

## Quick start (on the host)

**1. Find your user id** (the app uses it so files are owned correctly):

```bash
id -u    # PUID  → 1000 on the host
id -g    # PGID  → 1000 on the host
```

**2. Edit `docker-compose.yml`** and set two things:

- `PUID` / `PGID` to the numbers above (already `1000`/`1000`).
- The **left** side of the library volume to your real Plex Concerts folder:

  ```yaml
  volumes:
    - /mnt/nas/media/concerts:/library   # ← your Plex "Concerts" TV library
    - ./config:/config
  ```

  The folder is created automatically if it doesn't exist.

**3. Build and start:**

```bash
docker compose up -d --build
```

**4. Open the UI:** http://localhost:8681 (or `http://<the host-ip>:8681`).

> The container listens on port **8080**; the compose file maps it to **8681** on
> the host (8080 was already taken on the host). Change only the left number in
> `"8681:8080"` if you want a different host port.

Check it's healthy:

```bash
curl -s http://localhost:8681/health
# {"ok":true,"yt_dlp":"2025.xx.xx"}
```

---

## Using it

1. **Paste a playlist or single-video URL** (YouTube or Vimeo).
2. **Show (artist)** = the Plex series name, e.g. `Band A`.
3. Pick a **Naming** mode:
   - **performance** *(default)* → bare year folders (no "Season"), files named by the
     performance itself: `Band A/2020/2020.04.24 - X72 - Foro Sol, Mexico City, Mexico - 001.mkv`.
     Any field it can't determine (tour, venue, city, state, country) drops out **with its
     separator**, so a bare-metadata video collapses to `2020/2020.04.24 - 001.mkv`. Videos that
     share a descriptor (e.g. every song from one night) get a 3-digit sequence and stay separate
     Plex episodes. Optional **Tour** field fills that slot (else the playlist title is used).
   - **sequence** → `Band A - S2023E01 - Title.mkv`, unique episode numbers per year by concert
     date. Classic "concerts as TV" layout with `Season <year>/` folders.
   - **datecode** → `Band A - S2023E0427 - Title.mkv`; the date *is* the episode number
     (collides if two videos share a date).
   - **episode** → `Band A - S72E001 - Title.mkv`, fixed order. Set a **Season**
     (e.g. `72` for the X72 tour). Tick *"Oldest video → E001"* to number oldest-first.
   - **date** → `Band A - 2023-04-27 - Title.mkv`; Plex sorts the season by date.
4. **Quality**: `max` (best available) or `compatible` (H.264/AAC ≤1080p for smooth
   Plex Direct Play).
5. Leave **Generate NFO metadata** and **Generate Plex artwork** ticked for the full, clean
   result. (**Rename by footage date** applies only to the datecode/episode/date modes —
   performance and sequence always date by the concert.)
6. **Start download** → you land on the job page with a live log and the file list.

**Tip:** try a tiny 2–3 video playlist first to see the shape before a 200-video tour.

---

## Page audio capture

For web players yt-dlp has no extractor for. Pick an audio preset, tick **Capture from the
page's audio player**, then **Preview** and **Start**. Encoreum opens the page in headless
Chromium, finds what the player loads, and saves it without re-encoding, using the same
folders and numbering as any other audio download. It handles plain audio files, unencrypted
HLS/DASH, whole files in page memory, and Media Source streams. Media Source streams only
exist as they play, so those captures take as long as the track, and an unfinished one is
discarded.

**One page per job.** It saves the players on the page you give it and doesn't follow links
to other pages.

**It won't** capture DRM-protected or encrypted audio, or get past bot checks and CAPTCHAs.
A refusal says so in the job's "What went wrong" card.

**Only save what you have the right to keep.** Many streaming sites' terms forbid downloading,
and this tool can't tell.

### Setup

Add to `.env`, then `docker compose up -d --build`:

```bash
# Run the browser without its sandbox. Read "Security" below first.
CHROMIUM_NO_SANDBOX=1
# Only if you capture from your own servers on private addresses:
PAGE_CAPTURE_ALLOW_HOSTS=192.168.0.10,.plex.direct
```

### Sites with a login

```bash
./scripts/page-login.sh https://example.com/account
```

This starts a temporary browser and prints its address. Open it, log in yourself (PIN and 2FA
included), **leave that tab open**, and press Enter in the terminal. The session is saved to
`config/sessions/<host>.json` and used automatically for pages on that host, and the temporary
browser is removed. The file holds live login tokens: it's mode 600, but your `config/` backups
contain it too. Deleting it stops Encoreum using the login. To revoke the login itself, sign the
device out on the site.

**Plex:** run it against `http://<server-ip>:32400/web/` and pick your profile there. Capture
page URLs must use that same host.

### Security

A captured page is untrusted input running in a real browser.

- **The sandbox.** Chromium's sandbox can't start under Docker's default confinement on hosts
  like Ubuntu with `apparmor_restrict_unprivileged_userns=1`, so capture needs
  `CHROMIUM_NO_SANDBOX=1` there. A browser exploit in a captured page could then reach what this
  container can: your library (read/write), saved sessions, and mounted secrets such as the Plex
  token. Capture from sites you trust. The browser only runs during a capture or its preview.
- **Private addresses are blocked** so a page can't steer the browser at your LAN. The exceptions
  in `PAGE_CAPTURE_ALLOW_HOSTS` apply only when the page itself is on that list.
- **The temporary login browser has no password** and is open to your LAN while it runs. Set
  `BIND=127.0.0.1` to restrict it if you're tunnelling in.
- **Logs leave out URL query strings**, where stream tokens usually live.

### Running it without the app

```bash
./scripts/capture-page-audio.py URL --list        # what would be saved
./scripts/capture-page-audio.py URL --out ~/audio
./scripts/capture-page-audio.py --help
```

Add `--play-selector 'CSS'` if a site's Play button isn't found. Tests, against a local fixture
site: `docker run --rm -v "$PWD/tests:/tests:ro" --entrypoint python3 encoreum:local /tests/test_page_capture.py`

---

## Finding a channel's live streams

Open **Streams** (top right) and paste a YouTube channel (URL, `@handle` or
`UC…` id). You get one timeline: past streams on the left, a **NOW** line, and
scheduled streams on the right with countdowns. Click any card (or dot) for
details and an action:

- **Live now → Record**, **Upcoming → Schedule recording** (the job waits until
  the stream starts), **Past → Download**. Each opens the normal download form
  with the link filled in, so Preview and your naming choices still apply.

How it finds them: it reads the channel's **Live** tab in one go (titles,
thumbnails, lengths, live/upcoming/past). That tab doesn't include dates, so
the newest streams get exact dates from the channel's feed and a few lookups;
older ones are placed by their order and marked **≈** until **Look up 50 more
dates** fills them in. Channels without a Live tab have their newest 50 uploads
checked one by one (about 2 minutes); **Check 50 more uploads** goes further
back. Lookups share Encoreum's YouTube rate limit, and results are remembered
per channel so reopening one is instant. No YouTube account is needed.

## Chapters and sponsor segments

Every download keeps the video's chapters (for concerts, usually one per song),
and Plex shows them as a chapter list. Encoreum never splits a show into
per-song files; `--split-chapters` is refused for that reason.

**Optional: SponsorBlock** (a free outside service at sponsor.ajay.app; no
account or key; not Claude/Anthropic). Viewers there flag sponsor reads and
self-promotion inside YouTube videos. Encoreum can **label** those spots as
chapters (`[SponsorBlock]: Sponsor`). It never cuts them out.

- **On a download:** tick *Mark sponsor segments as chapters*. Preview checks
  each item and tells you if any have segments.
- **On files you already have:** open **Sponsors** (top right), pick a
  destination and press **Scan**. For each hit, **Add marks** rewrites only the
  file's chapter list (no re-download, nothing cut) after saving the original
  chapters, and **Undo** puts them back.

What to expect: an ad in the middle of a song's chapter splits that chapter
around it, and a video with no chapters gets three (title, Sponsor, title).
Plex can't show a Skip button for these; jump past them in the chapter list.
Most full-concert uploads have no sponsor segments at all. If SponsorBlock is
down, Encoreum says so and everything else works as normal. Lookups send only a
short hash prefix of each video ID. SponsorBlock data is CC BY-NC-SA 4.0
(non-commercial, credited on screen).

## One-time Plex setup

**No third-party plugin needed.** Plex Media Server **1.43.1+** ships an official
native NFO agent that reads the metadata Encoreum writes. (The old
`XBMCnfoTVImporter` plugin is abandoned — last real commit 2019 — and runs on the
legacy plugin framework Plex is actively removing, so don't use it.)

1. Plex → **Settings → Libraries → Add Library → TV Shows**, name it `Concerts`,
   point it at your Concerts folder.
2. Open **Advanced** and set **Agent → `Plex NFO Series`**. Leave the Scanner on
   *Plex TV Series*.
3. Save, then **Scan Library Files**.

That's it — you get the full episode **plots, genres, air dates, cast, and posters**
from the `.nfo` and artwork Encoreum generates. Full background (and the folder/naming
"why") is in [`docs/Plex-Concerts-as-TV-Guide.md`](docs/Plex-Concerts-as-TV-Guide.md).

> **How the default *performance* layout maps to Plex.** The bare **year folder** is the
> season, and each episode's **number comes from its `.nfo`** (a unique, date-ordered index
> within that year — so several videos sharing one date never merge). This is exactly why the
> `Plex NFO Series` **agent** matters here: with no `SxxExx` in the filename, the NFO is what
> supplies season + episode. Keep the Scanner on *Plex TV Series*.

> On 1.43.0+ Plex hides *legacy* agents during library setup; the native
> `Plex NFO Series` agent is the supported path. If you're on an older PMS, either
> update Plex, or fall back to filenames + embedded tags (you'll lose the rich plots).
> Encoreum also writes Plex-native posters/thumbnails, so even without any NFO agent
> the library still shows artwork, titles, seasons, episode numbers, and dates.

---

## Keeping it up to date

- **yt-dlp** updates itself (start, daily, and the **Update now** button). Nothing to do.
- **ffmpeg and the base OS** update when you rebuild the image:

  ```bash
  docker compose build --pull && docker compose up -d
  ```

---

## Configuration reference

| Setting (env in `docker-compose.yml`) | Meaning | Default |
|---|---|---|
| `PUID` / `PGID` | Host user/group that owns the library (so Plex can read files) | `1000` |
| `TZ` | Timezone for logs/timestamps | `America/New_York` |
| `ALLOWED_ROOTS` | Comma-separated folders the app is allowed to write into | `/library` |
| volume `…:/library` | Your Plex Concerts folder | `/mnt/nas/media/concerts` |
| volume `./config:/config` | Job history, logs, per-show download-archives, yt-dlp venv | — |

**Ports:** container `8080`, mapped to host `8681`.

---

## How it's built

- **FastAPI + Uvicorn**, Jinja2 templates, a tiny threaded job runner (no external queue).
- **SQLite** job store at `/config/jobs.db`; logs at `/config/logs/<id>.log`;
  per-show resume archives at `/config/archives/<Show>.txt`.
- **yt-dlp** lives in a virtual-env at `/config/venv` owned by your `PUID`, which is
  what lets the (non-root) app upgrade it on demand.
- The download step is a faithful port of `scripts/example-tour-download.sh`; the
  rename and NFO steps call `scripts/rename-by-footage-date.py` and
  `scripts/generate-nfo.py` unchanged. A final step calls
  `scripts/generate-plex-art.py` to emit Plex-native posters from the episode thumbnails.

```
encoreum/
├─ docker-compose.yml   Dockerfile   entrypoint.sh   requirements.txt
├─ app/
│  ├─ main.py       FastAPI routes + live-log SSE
│  ├─ config.py     paths, defaults, the write-guardrail
│  ├─ jobs.py       SQLite store + background runner + cancel
│  ├─ pipeline.py   builds the yt-dlp command; runs download → rename → nfo → artwork
│  ├─ updater.py    yt-dlp self-update
│  ├─ templates/    index.html, job.html
│  └─ static/app.css
└─ scripts/         rename-by-footage-date.py, generate-nfo.py (unchanged),
                    generate-plex-art.py, example-tour-download.sh + the guide
```

---

## Troubleshooting

- **`/health` fails / page won't load** → `docker compose logs -f encoreum`.
- **Downloads fail with odd YouTube errors** → click **Update now** (or wait for the
  daily update); YouTube changed something and yt-dlp needs the newest version.
- **Files owned by the wrong user in Plex** → set `PUID`/`PGID` to `id -u` / `id -g`
  of the user Plex runs as, then `docker compose up -d`.
- **"Refusing to write … outside ALLOWED_ROOTS"** → the show/subfolder resolved
  outside `/library`. Keep the show name simple (no leading `/` or `..`).
- **Members-only / age-restricted video** → set *Cookies from browser* to the browser
  you're logged into (only works if that browser profile is reachable in the container;
  on a headless server this usually isn't — download those by hand).

## License

Encoreum is free software under the **GNU Affero General Public License v3.0**
(see `LICENSE`). You may use, change and share it; if you run a modified version
as a service other people use over a network, you must offer them its source.

It calls other tools rather than including them (yt-dlp, ffmpeg, streamlink,
Chromium/Playwright), each under its own license. Optional online services it
can use have their own terms: SponsorBlock data is CC BY-NC-SA 4.0
(non-commercial, credited in the app), and setlist.fm's API is for
non-commercial use with attribution.
