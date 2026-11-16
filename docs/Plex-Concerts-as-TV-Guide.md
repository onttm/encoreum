# Concerts as TV Shows in Plex — Setup & Workflows

A complete framework for storing concerts and music videos as **TV series** in Plex,
plus a repeatable way to build new concert series from YouTube and Vimeo.

---

## 1. The idea (how everything maps)

| TV concept | Concert concept | Example |
|------------|-----------------|---------|
| Series / Show | **Artist** | Band A |
| Season | **Tour or Year** | X72 tour → *Season 72* |
| Episode | **One concert / video** | *S72E001 – Amsterdam, Night 1* |

Everything below is just a way of putting your files into that shape so Plex's
TV scanner recognises them.

**Target folder layout** (this is the whole trick — get the folders and filenames
right and Plex does the rest):

```
Concerts/                         <- your Plex "TV Shows" library root
└── Band A/                    <- the show
    ├── Season 72/                <- the tour (X72)
    │   ├── Band A - S72E001 - Amsterdam Night 1.mkv
    │   ├── Band A - S72E002 - Amsterdam Night 2.mkv
    │   └── ...
    └── Season 75/                <- a later tour becomes another season
        └── ...
```

---

## 2. One-time Plex setup

1. In Plex, go to **Settings → Libraries → Add Library**.
2. Choose type **TV Shows**. Name it something like **Concerts**.
3. Point it at the `Concerts/` folder above.
4. Under **Advanced**:
   - **Scanner:** *Plex TV Series* (the default).
   - **Agent:** *Plex TV Series*. Turn **ON** "Local Media Assets" if listed —
     this lets Plex use the poster/thumbnail and info stored with your files.
5. Save, then **Scan Library Files**.

To make a season sort by concert **date** instead of episode number: open the
season in Plex → **⋯ → Edit → Advanced → Episode Sorting → Oldest first**, and it
will follow the air date it read from the filename (date mode) or the episode
number (episode mode).

---

## 3. Flow B — Build a new concert series from YouTube / Vimeo

This is the flow for your Band A X72 playlist, and it works for any artist
later. It uses **yt-dlp** (which you already installed) plus **ffmpeg**.

### 3a. Install the one missing piece (best-quality merging)

```
sudo apt install ffmpeg
```

ffmpeg is what lets yt-dlp combine the best video stream and the best audio
stream into one file. Without it you'll be capped at lower quality.

### 3b. Preview the playlist first (recommended)

Before downloading 200 videos, look at the order so you know whether to reverse
it. This just lists them, it downloads nothing:

```
yt-dlp --flat-playlist --print "%(playlist_index)s: %(title)s" \
  "https://www.youtube.com/playlist?list=PLJvQXRgtxlukmDarotT2ZFhjAVYdQHSka"
```

If item **1** is the **newest** concert, keep `REVERSE=true` in the script so the
oldest becomes E001. If item 1 is already the oldest, set `REVERSE=false`.

### 3c. Run the download script

Use the included **`example-tour-download.sh`**. Open it, edit only the CONFIG
block at the top (especially `LIBRARY=` → your real Plex path), then:

```
chmod +x example-tour-download.sh
./example-tour-download.sh
```

It saves into `Band A/Season 72/` and names each file
`Band A - S72E001 - <video title>.mkv`, oldest to newest. You can stop it any
time and re-run — finished episodes are skipped and the numbering stays put.

### 3d. Naming modes (episode number vs. date vs. date-code)

The script has a `NAMING=` switch with three choices:

- **`NAMING="episode"`** → `Band A - S72E001 - Title.mkv`. Most reliable
  ordering. **You don't type any numbers** — the script assigns them from playlist
  position. One season (S72) for the whole tour.
- **`NAMING="date"`** → `Band A - 2023-04-27 - Title.mkv`. No episode numbers;
  Plex reads the `YYYY-MM-DD` date and orders the season by date automatically.
- **`NAMING="datecode"`** → `Band A - S2023E0427 - Title.mkv`. **Season = the
  year, Episode = MMDD.** This is the scheme where the date *is* the episode number:
  it sorts chronologically, Plex parses it reliably, and it's self-documenting.

Important caveat about dates: use the **`YYYY-MM-DD`** pattern (with dashes) for
date mode, or the `SyyyyEmmdd` code for datecode mode. A bare `YYYYMMDD` prefix
(e.g. `20230427`) is *not* recognised by Plex and the episodes won't sort or match.

Two things to know about datecode:

- **Each year becomes its own season.** The X72 tour runs 2023–2024, so it splits
  into `Season 2023` and `Season 2024`. That's inherent to date-coded seasons.
- **Two concerts on the exact same date collide** (same `S2023E0427`). Rare for one
  band; the renamer below flags any duplicates so you can nudge one by hand.

### 3d-b. Getting the *footage* date, not the upload date

`NAMING="datecode"` in the download script uses the video's **upload/release date**
— which is usually close to the concert date but not guaranteed. To key episodes on
the **actual concert (footage) date**, use the companion script
**`rename-by-footage-date.py`**. It reads the date out of each video's title or
description (understands both `2023-04-27` and "April 27, 2023" styles), falls back
to the upload date only when nothing is found, and renames files to
`Band A - S2023E0427 - Title.mkv` inside `Season 2023/` folders.

```
# Always preview first (changes nothing):
python3 rename-by-footage-date.py "/path/to/Concerts/Band A" --show "Band A"

# When the preview looks right:
python3 rename-by-footage-date.py "/path/to/Concerts/Band A" --show "Band A" --apply
```

It works because the download script writes a small `.info.json` beside each video.
Any file whose date it can't find is listed at the end so you can fix those few by
hand. If your concerts' titles use an unusual date format, tell me what they look
like (run the preview command in 3b to dump the titles) and I'll widen the parser.

### 3e. Metadata: what the download captures, and full NFO

The download script already pushes yt-dlp hard for local metadata:

- **`--embed-metadata`** writes title, description and date *inside* each `.mkv`.
- **`--embed-thumbnail` + `--write-thumbnail --convert-thumbnails jpg`** stores the
  cover art in the file *and* as a `.jpg` beside it (Plex reads jpg as the episode
  image; it can't use YouTube's default `.webp`).
- **`--embed-chapters`** keeps the concert's song markers.
- **`--write-info-json`** saves the full metadata dump next to each file.
- **`-S` format sorting** picks the best (or most Plex-compatible) stream — see 3h.

Embedded description/title work in the player, but Plex's TV view shows episode
**plots** most reliably from **NFO files**, not embedded tags. Since you want solid,
complete local metadata, use the NFO builder below.

### 3f. Full local metadata with NFO (recommended for "complete" metadata)

Run **`generate-nfo.py`** after downloading (and after the footage-date rename, if
you use it). It turns each `.info.json` into thorough Plex/Kodi NFO:

```
python3 generate-nfo.py "/path/to/Concerts/Band A" --show "Band A"
```

It writes, for the whole show:

- `tvshow.nfo` — series title, plot, genres, studio, first-aired year.
- `Season NNNN/season.nfo` — one per season.
- one `…​.nfo` per episode with **title, show title, season, episode, full plot,
  aired + premiered date, year, studio, director, genres, a "Live" tag, runtime,
  the episode thumbnail, the YouTube id, and the source URL.**

Options: `--genre "Rock"` (repeatable), `--studio "..."`, `--plot "series blurb"`,
and `--overwrite` to refresh existing NFO. It only reads `.info.json` and writes
`.nfo`; it never touches your videos.

**One requirement — Plex must be told to read NFO — but this is now built in.**
Plex Media Server **1.43.1+** ships an official native NFO agent, so **no plugin is
needed**:

1. On your Concerts library: **Edit → Advanced → Agent → `Plex NFO Series`**
   (leave the Scanner on *Plex TV Series*).
2. **Scan Library Files.**

That's the whole setup — Plex reads the Kodi/XBMC tags this script writes (`<title>`,
`<plot>`, `<aired>`, `<premiered>`, `<year>`, `<genre>`, `<studio>`, `<director>`,
`<runtime>`, `<uniqueid>`, `<thumb>`, `<tag>`), matching each episode NFO to its video
by filename.

> **Don't use the old `XBMCnfoTVImporter` plugin.** It's abandoned (last real commit
> 2019) and runs on Plex's legacy plugin framework, which Plex is removing — on 1.43.0+
> legacy agents are even hidden during library setup. The native `Plex NFO Series`
> agent replaces it.

Without any NFO agent you still keep everything — Plex falls back to the filename
(titles/seasons/episodes/dates) and the local artwork from `generate-plex-art.py`
(posters + thumbnails), and just skips the rich plots.

### 3g. Quality vs. compatibility

The `QUALITY=` switch controls yt-dlp's stream ranking:

- **`QUALITY="max"`** — highest resolution/bitrate available. On YouTube that's
  often 4K VP9 or AV1. A few older Plex client devices have to transcode AV1.
- **`QUALITY="compatible"`** — prefers H.264 + AAC up to 1080p, which most Plex
  clients play back with zero transcoding ("Direct Play").

Two more optional switches: `WRITE_SUBS=true` pulls English captions as `.srt`
sidecars (off by default — auto-captions on live music are poor), and
`COOKIES_BROWSER="firefox"` lets yt-dlp use your logged-in browser session for
age-restricted or members-only videos.

### 3h. Vimeo

Identical process — yt-dlp supports Vimeo. Just put a Vimeo playlist or video URL
in `PLAYLIST=`. Everything else (naming, seasons, metadata, NFO) is the same.

### 3i. Adding future tours as new seasons

For the next tour, copy the script, change `SEASON=` (or use datecode mode, which
seasons by year automatically) and `PLAYLIST=`, and run it. It drops into a new
season folder under the same `Band A/` show. Re-run `generate-nfo.py` afterward
to refresh metadata.

---

## 4. Flow A — Migrate your existing concerts and music videos

Right now your concerts live as **movies**. Moving them into the TV shape is just
folder + filename restructuring — no re-downloading.

### 4a. Concerts currently stored as movies

Target, per artist:

```
Concerts/<Artist>/Season <tour-or-year>/<Artist> - S<season>E<nn> - <Concert name>.mkv
```

Two ways to do it:

- **By hand** for a handful: create the `Artist/Season NN/` folders and rename
  files to the `Artist - SxxExx - Title` pattern shown above.
- **Automated** for many: I can generate a rename script (or a spreadsheet-driven
  renamer) that reads your current filenames and moves them into place. For that I
  need to see your current folder — in this app you can grant me access to a folder
  and I'll build the exact renamer for your real files. Just say the word and I'll
  request folder access.

Tip: keep the **Concerts** library separate from your regular Movies/TV libraries
so the scanner rules stay clean.

### 4b. Music videos

Music videos fit the same model, with two sensible options:

- **One "Music Videos" show**, seasons by artist or by year, each clip an episode.
  Good if you have a mixed grab-bag.
- **Per-artist series**, matching your concerts (e.g. a Band A show whose
  Season 0 or "Videos" season holds official videos). Good if you want everything
  for an artist in one place.

Either works — pick based on how you like to browse. I can bake whichever you
choose into the renamer.

---

## 5. Quick troubleshooting

- **Only getting low resolution** → ffmpeg isn't installed. `sudo apt install ffmpeg`.
- **`429` / "Too Many Requests"** → the script already sleeps between requests; if
  it still happens, wait a while and re-run (it resumes).
- **A video is private/removed** → the script skips it and leaves that episode
  number as a gap; the rest keep their correct numbers.
- **Episodes not grouped as a show** → check the filename has `SxxExx` (episode
  mode) or `YYYY-MM-DD` with dashes (date mode), and that the season folder is
  named exactly `Season 72`.
- **Wrong order (newest first)** → flip `REVERSE` in the script, delete the
  archive file, and re-run.

---

## 6. Files in this package

- **`example-tour-download.sh`** — the ready-to-run downloader/namer for the X72
  playlist (edit the CONFIG block, then run). Supports episode / date / datecode,
  quality vs. compatibility, subtitles, and browser cookies.
- **`rename-by-footage-date.py`** — renames downloaded files to `S<YYYY>E<MMDD>`
  using the true concert date found in each video's title/description.
- **`generate-nfo.py`** — builds complete Plex/Kodi NFO (tvshow, season, and full
  per-episode metadata) from the `.info.json` files.
- **`generate-plex-art.py`** — emits Plex-native posters (`poster.jpg`, `fanart.jpg`,
  season posters) from the episode thumbnails, so artwork shows even with no NFO agent.
- **`Plex-Concerts-as-TV-Guide.md`** — this document.

## 7. The full pipeline at a glance

```
1. ./example-tour-download.sh                                  # download + thumbnails + info.json
2. python3 rename-by-footage-date.py "…/Band A" --show Band A          # (optional) true-date codes
3. python3 generate-nfo.py "…/Band A" --show Band A       # rich local metadata
4. python3 generate-plex-art.py "…/Band A" --show Band A  # Plex-native posters
5. Plex: set library Agent = "Plex NFO Series" (PMS 1.43.1+) → Scan Library Files
```

*(Encoreum runs steps 1–4 for you inside the container; step 5 is the one-time
Plex library setting.)*
