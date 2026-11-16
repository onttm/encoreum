#!/usr/bin/env python3
"""
rip-concert-audio.py -- a static-image "video" is an audio bootleg. Treat it as one.

THE SHAPE OF THE PROBLEM
    A large slice of live music on YouTube is a still image with a full concert
    under it. As video it is worthless — one JPEG stretched over 80 minutes. As
    audio it is a complete show, and very often the uploader has typed the
    setlist with timestamps into the description, which is a track index someone
    made by hand and gave away.

    This turns that into a tagged, split album in the Live Music library.

WHY NOT JUST USE yt-dlp --split-chapters
    Because it silently does nothing here. yt-dlp only recognises a narrow set
    of description timestamp layouts, and the real world does not cooperate.
    Verified against the video this was written for: 16 clean timestamps in the
    description, and yt-dlp reported `chapters: 0`, because the lines are
    numbered "1) 0:00 - Opening Song" and the leading "1)" defeats its parser.

    So chapters are read from yt-dlp WHEN IT FINDS THEM — it is right more often
    than a bespoke parser and costs nothing to check — and parsed from the
    description when it does not. The fallback is not the exception; on hand-
    typed setlists it is the common case.

THE DATE IS IN THE TITLE, NOT THE UPLOAD
    upload_date is when someone posted it, which for archival concert audio is
    routinely decades after the show. The video this was built against was
    performed 1995-10-21 and uploaded 2019-12-09 — a 24-year gap. Filing it
    under the upload date puts a 1995 Band B show in a 2019 folder,
    which is the same class of error that produced `Season 2019` for 1992 Band C
    concerts in this library already.

    So the performance date is parsed from the TITLE, and if it cannot be found
    the script says so and asks rather than falling back to upload_date. A wrong
    date is worse than a missing one because nothing later looks wrong.

LOSSY SOURCE STAYS LOSSY
    YouTube audio is Opus or AAC. Transcoding that to FLAC inflates it five to
    tenfold and recovers nothing — the information is already gone. Tracks are
    split with a STREAM COPY, so the bytes are the ones YouTube served, cut at
    the timestamps. Use --flac only if something downstream genuinely requires
    it, and know that it is a lossless container around lossy audio.

USAGE
    ./scripts/rip-concert-audio.py URL                      # show the plan
    ./scripts/rip-concert-audio.py URL --apply              # rip it
    ./scripts/rip-concert-audio.py URL --artist "..." --date 1995-10-21 --apply
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

YTDLP = os.environ.get("YTDLP_BIN", shutil.which("yt-dlp") or "yt-dlp")
FFMPEG = os.environ.get("FFMPEG_BIN", shutil.which("ffmpeg") or "ffmpeg")
LIVE_AUDIO_DIR = Path(os.environ.get("LIVE_AUDIO_DIR", "/library/live-music"))

# A timestamp: 0:00, 12:34, 1:03:53
_TS = r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})"

# Layouts seen in the wild. Ordered most-specific first; the leading-index form
# ("1) 0:00 - Title") is what defeats yt-dlp and is extremely common on
# hand-typed setlists.
_LINE_PATTERNS = [
    re.compile(rf"^\s*\d{{1,3}}\s*[.)\]]\s*{_TS}\s*[-–—:]?\s*(?P<title>.+?)\s*$"),
    re.compile(rf"^\s*\[?{_TS}\]?\s*[-–—:]?\s*(?P<title>.+?)\s*$"),
    re.compile(rf"^\s*(?P<title>.+?)\s*[-–—:]?\s*\[?{_TS}\]?\s*$"),
]

# Lines that are structure or chatter, never songs.
_SKIP = re.compile(
    r"^\s*(?:$|#|//|subscribe|follow|support|patreon|instagram|twitter|"
    r"facebook|tracklist|setlist|track ?list|lineup|personnel|credits?|"
    r"recorded|taped|source|lineage|transfer|http)", re.I)


def _secs(h, m, s) -> int:
    return int(h or 0) * 3600 + int(m) * 60 + int(s)


def probe(url: str) -> dict:
    """One yt-dlp metadata call. No download."""
    r = subprocess.run([YTDLP, "--no-warnings", "-j", "--skip-download",
                        "--ignore-no-formats-error", url],
                       capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise SystemExit(f"yt-dlp could not read that URL:\n{r.stderr.strip()[:400]}")
    for line in r.stdout.splitlines():
        if line.lstrip().startswith("{"):
            return json.loads(line)
    raise SystemExit("yt-dlp returned no metadata")


def parse_timestamps(text: str, duration: int) -> list[dict]:
    """Pull a track index out of a description.

    Two rules keep this from inventing tracks out of prose:
      * timestamps must be NON-DECREASING — a description that mentions "see
        2:30 of my other video" breaks monotonicity and is discarded;
      * a title must survive cleaning and not look like boilerplate.
    """
    rows = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or _SKIP.match(line):
            continue
        for pat in _LINE_PATTERNS:
            m = pat.match(line)
            if not m:
                continue
            start = _secs(*m.group(1, 2, 3))
            title = clean_title(m.group("title"))
            if not title or len(title) > 140:
                break
            rows.append({"start": start, "title": title, "raw": line})
            break

    # Keep the longest non-decreasing run. A stray timestamp in a sign-off ("go
    # to 2:30") would otherwise shift every track after it.
    best: list[dict] = []
    cur: list[dict] = []
    for r in rows:
        if cur and r["start"] < cur[-1]["start"]:
            best = max(best, cur, key=len)
            cur = []
        cur.append(r)
    best = max(best, cur, key=len) if cur else best

    out = []
    for i, r in enumerate(best):
        end = best[i + 1]["start"] if i + 1 < len(best) else duration
        if end - r["start"] < 20:        # a 20s "track" is a mis-parse
            continue
        out.append({"position": len(out) + 1, "title": r["title"],
                    "start": r["start"], "end": end,
                    "seconds": end - r["start"], "raw": r["raw"]})
    return out


def clean_title(t: str) -> str:
    t = (t or "").strip(" -–—:·|\t")
    t = re.sub(r"^\d{1,3}\s*[.)\]]\s*", "", t)      # leftover index
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip(" -–—:·|")


def guest_of(title: str) -> tuple[str, str | None]:
    """Split "Reptile [featuring Guest Artist]" into the song and the guest.

    Worth keeping separate rather than leaving in the title: a guest appearance
    is a fact about the performance, and burying it in the song name means the
    track no longer matches the song it actually is.
    """
    m = re.search(r"[\[\(]\s*(?:feat(?:uring)?\.?|w(?:ith)?\.?)\s+([^\])]+)[\])]\s*$",
                  title, re.I)
    if not m:
        return title, None
    return title[:m.start()].strip(" -–—"), m.group(1).strip()


# --------------------------------------------------------------------------
# what the title is telling us
# --------------------------------------------------------------------------

_MONTHS = ("january february march april may june july august september "
           "october november december").split()


def parse_title(title: str) -> dict:
    """Artist / date / venue out of a YouTube title.

    Deliberately conservative. Everything it returns is a SUGGESTION shown to a
    person before anything is written, because a title is free text and a
    confident wrong parse is worse than an empty field.
    """
    out = {"artist": None, "date": None, "venue": None, "guests": None,
           "release": None}
    t = title

    # date: 10/21/1995, 1995-10-21, 21 October 1995, October 21, 1995
    m = re.search(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})", t)
    if m:
        mm, dd, yy = m.groups()
        out["date"] = f"{yy}-{int(mm):02d}-{int(dd):02d}"
    if not out["date"]:
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
        if m:
            out["date"] = m.group(0)
    if not out["date"]:
        m = re.search(rf"(\d{{1,2}})\s+({'|'.join(_MONTHS)})\w*\s+(\d{{4}})", t, re.I)
        if m:
            out["date"] = (f"{m.group(3)}-{_MONTHS.index(m.group(2).lower())+1:02d}"
                           f"-{int(m.group(1)):02d}")
    if not out["date"]:
        m = re.search(rf"({'|'.join(_MONTHS)})\w*\s+(\d{{1,2}}),?\s+(\d{{4}})", t, re.I)
        if m:
            out["date"] = (f"{m.group(3)}-{_MONTHS.index(m.group(1).lower())+1:02d}"
                           f"-{int(m.group(2)):02d}")

    # venue: "Live at X", "@ X", "Live from X"
    m = re.search(r"\b(?:live\s+(?:at|from)|@)\s+([^,\(\)\[\]]+)", t, re.I)
    if m:
        v = m.group(1).strip()
        v = re.split(r"\s+\d{1,2}[/.]\d{1,2}[/.]\d{4}|\s+\d{4}-\d{2}-\d{2}", v)[0]
        out["venue"] = v.strip(" -–—,")

    # release name: bootlegs are usually titled "Artist - NAME (Live at ...)",
    # and NAME is what the recording is actually CALLED in the community that
    # circulates it. Throwing it away and filing the show as a date loses the
    # name people would search for — this one is "The Oddball Couple".
    m = re.match(r"^[^-–—]+[-–—]\s*(?P<rel>[^(\[]+?)\s*[\(\[]", t)
    if m:
        rel = m.group("rel").strip(" -–—")
        # Guard against "Artist - Live at Venue" where the middle is not a name.
        if rel and not re.match(r"^(?:live|full (?:concert|show|set)|concert|"
                                r"audio|complete)\b", rel, re.I) and len(rel) <= 80:
            out["release"] = rel

    # artist: everything before the first strong separator, minus parentheticals
    head = re.split(r"\s+[-–—]\s+|\s*[\(\[]", t)[0].strip()
    m = re.match(r"^(.*?)\s+(?:with|feat(?:uring)?\.?|&|\+)\s+(.+)$", head, re.I)
    if m:
        out["artist"], out["guests"] = m.group(1).strip(), m.group(2).strip()
    else:
        out["artist"] = head or None
    return out


# --------------------------------------------------------------------------
# findability
# --------------------------------------------------------------------------

def plex_playlist(meta: dict, tracks: list[dict], timeout: int = 180) -> str | None:
    """Create a Plex playlist titled with the guest, so the show is findable.

    WHY THIS IS NEEDED, measured rather than assumed
        Plex indexes a track credit for search ONLY when it is a bare artist
        name it can resolve. A compound credit — "Band B with Guest Artist", and equally the slash and semicolon forms — is stored and
        DISPLAYED correctly but never indexed. Verified by A/B test on fresh
        albums, with `Prefer local metadata` on:

            ARTIST "Guest Artist"                        -> searchable
            ARTIST "Band B with Guest Artist"   -> NOT searchable
            ARTIST "Band B/Guest Artist"        -> NOT searchable
            ARTIST "Band B;Guest Artist"        -> NOT searchable

        Album COLLECTIONS are not searchable either — the tag applies and
        returns nothing.

        So there is a real choice, and this takes the honest side of it: the
        per-track credit stays exact, because the tag is the permanent record
        that travels with the file, and findability is added alongside via a
        playlist, whose TITLE is searchable. Writing a bare "Guest Artist" onto
        five Band B songs would buy search by making the file lie.

    Optional and non-fatal. With no PLEX_URL/PLEX_TOKEN the rip is unaffected —
    the album is complete and correctly tagged either way.
    """
    import urllib.error
    import urllib.request as _u
    url = os.environ.get("PLEX_URL", "").rstrip("/")
    token = os.environ.get("PLEX_TOKEN", "")
    if not url or not token:
        print("    (no PLEX_URL/PLEX_TOKEN — skipping the playlist; add one by "
              "hand to make the guest searchable)")
        return None

    guests = sorted({g for g in (guest_of(t["title"])[1] for t in tracks) if g})
    if not guests:
        return None

    import xml.etree.ElementTree as ET

    def call(path, method="GET", **q):
        q["X-Plex-Token"] = token
        req = _u.Request(f"{url}{path}?{urllib.parse.urlencode(q)}", method=method)
        try:
            with _u.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception:  # noqa: BLE001
            return b""

    try:
        mid = ET.fromstring(call("/")).get("machineIdentifier")
        secs = [d.get("key") for d in ET.fromstring(call("/library/sections"))
                .findall("Directory") if d.get("type") == "artist"]
        # The album has to be scanned before a playlist can point at it, so ask
        # for a scan and then wait for it to appear rather than assuming.
        for k in secs:
            call(f"/library/sections/{k}/refresh")
        rk = None
        waited = 0
        while waited < timeout and not rk:
            time.sleep(10)
            waited += 10
            for k in secs:
                for a in ET.fromstring(call(f"/library/sections/{k}/all", type="9")) \
                        .findall("Directory"):
                    if a.get("title") == meta["album"]:
                        rk = a.get("ratingKey")
                        break
                if rk:
                    break
        if not rk:
            print(f"    (album not scanned within {timeout}s — playlist skipped; "
                  "re-run with --playlist-only once Plex has seen it)")
            return None

        title = f"{meta['artist']} with {', '.join(guests)}"
        where = meta.get("venue") or meta.get("festival")
        if where:
            title += f" - {where}"
        if meta.get("date"):
            title += f" {meta['date']}"
        call("/playlists", "POST", type="audio", title=title, smart="0",
             uri=f"server://{mid}/com.plexapp.plugins.library/library/metadata/{rk}")
        print(f"    playlist: {title!r}")
        print(f"      -> makes this findable by searching {guests[0]!r}, which the "
              "track credit alone does NOT do")
        return title
    except Exception as e:  # noqa: BLE001
        print(f"    (playlist step failed: {e}; the album itself is fine)")
        return None


def _same_source(album_dir: Path, source_type: str) -> bool:
    """Is an existing album folder the SAME recording source as this one?

    Re-ripping the same source should overwrite in place; a DIFFERENT source of
    the same show is a separate album and must not collide.
    """
    try:
        from mutagen import File as MFile
    except ImportError:
        return True
    for f in sorted(album_dir.iterdir()):
        if f.suffix.lower() not in (".m4a", ".mp4", ".flac", ".opus"):
            continue
        try:
            au = MFile(str(f))
            if au is None:
                continue
            for key in ("SOURCE_TYPE", "----:com.apple.iTunes:SOURCE_TYPE",
                        "source_type"):
                v = au.get(key)
                if v:
                    got = v[0]
                    got = bytes(got).decode() if isinstance(got, bytes) else str(got)
                    return got.strip().upper() == source_type.strip().upper()
        except Exception:  # noqa: BLE001
            continue
    return True


def _safe(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]', " ", s or "").strip()
    return re.sub(r"\s{2,}", " ", s)[:120].rstrip(" .")


def hms(sec: int) -> str:
    h, r = divmod(int(sec), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# --------------------------------------------------------------------------
# ripping
# --------------------------------------------------------------------------

def download_audio(url: str, workdir: Path, want_flac: bool) -> Path:
    """Fetch the audio stream once. Prefer m4a: it stream-copies cleanly."""
    out = str(workdir / "source.%(ext)s")
    cmd = [YTDLP, "--no-warnings", "-f", "bestaudio[ext=m4a]/bestaudio/best",
           "-o", out, url]
    if want_flac:
        cmd += ["-x", "--audio-format", "flac"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    if r.returncode != 0:
        raise SystemExit(f"download failed:\n{r.stderr.strip()[:400]}")
    files = [p for p in workdir.iterdir() if p.stem == "source"]
    if not files:
        raise SystemExit("download produced no file")
    return files[0]


def crop_borders(jpg: Path) -> Path:
    """Cut the pillarbox/letterbox bars off a thumbnail.

    A static-image video is nearly always a SQUARE sleeve pillarboxed into 16:9
    — YouTube's frame is 1280x720 and the artwork sits in the middle with black
    down both sides. Handing that to Plex as an album poster wastes 44% of the
    image on bars and shows the sleeve shrunk inside a letterbox.

    Measured on the Band B/Guest Artist video: the 1280x720 thumbnail cropped to exactly
    720x720 at x=280 — the original bootleg sleeve, recovered whole.

    cropdetect needs motion, so the still is LOOPED to give it frames; run
    against a single image it silently reports nothing. Anything that does not
    detect, or that would cut more than half the picture away, is left alone —
    a wrong crop is worse than bars.
    """
    try:
        r = subprocess.run(
            [FFMPEG, "-loop", "1", "-i", str(jpg), "-frames:v", "4",
             "-vf", "cropdetect=24:2:0", "-f", "null", "-"],
            capture_output=True, text=True, timeout=60)
        m = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", r.stderr or "")
        if not m:
            return jpg
        w, h, x, y = (int(v) for v in m[-1])
        probe = subprocess.run(
            [FFMPEG.replace("ffmpeg", "ffprobe"), "-v", "error", "-select_streams",
             "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0",
             str(jpg)], capture_output=True, text=True, timeout=30)
        sw, sh = (int(v) for v in probe.stdout.strip().split(",")[:2])
        if w * h < (sw * sh) // 2 or (w, h) == (sw, sh):
            return jpg                      # nothing to gain, or too aggressive
        out = jpg.with_name("cover-cropped.jpg")
        c = subprocess.run(
            [FFMPEG, "-y", "-loglevel", "error", "-i", str(jpg),
             "-vf", f"crop={w}:{h}:{x}:{y}", "-q:v", "2", str(out)],
            capture_output=True, text=True, timeout=60)
        if c.returncode == 0 and out.exists():
            print(f"    cropped artwork {sw}x{sh} -> {w}x{h} "
                  "(removed pillarbox bars)")
            return out
    except Exception:  # noqa: BLE001
        pass
    return jpg


def fetch_cover(url: str, workdir: Path) -> Path | None:
    """Grab the video's still image and use it as album art.

    Worth doing precisely because of what this tool targets: a static-image
    concert video IS a cover image with audio attached. The single frame that
    makes the video worthless is the artwork the album wants — usually the
    original poster, ticket stub or sleeve the uploader chose.
    """
    base = str(workdir / "cover")
    r = subprocess.run([YTDLP, "--no-warnings", "--skip-download",
                        "--write-thumbnail", "--convert-thumbnails", "jpg",
                        "-o", base + ".%(ext)s", url],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        return None
    for p in sorted(workdir.glob("cover*.jpg")):
        return crop_borders(p)
    return None


def embed_cover(path: Path, jpg: Path) -> None:
    """Embed art in the file as well as writing cover.jpg beside it.

    Both, because they serve different readers: Plex picks up cover.jpg from the
    album folder, while PlexAmp offline caches and most other players read the
    embedded picture. A folder image alone travels badly once a file is copied
    somewhere else.
    """
    try:
        data = jpg.read_bytes()
        if path.suffix.lower() == ".flac":
            from mutagen.flac import FLAC, Picture
            au = FLAC(str(path))
            pic = Picture()
            pic.type, pic.mime, pic.data = 3, "image/jpeg", data
            au.clear_pictures()
            au.add_picture(pic)
            au.save()
        else:
            from mutagen.mp4 import MP4, MP4Cover
            au = MP4(str(path))
            au["covr"] = [MP4Cover(data, imageformat=MP4Cover.FORMAT_JPEG)]
            au.save()
    except Exception:  # noqa: BLE001
        pass


def split_track(src: Path, dest: Path, start: int, end: int, reencode: bool) -> bool:
    """Cut one track. Stream copy unless the source had to be re-encoded.

    -ss BEFORE -i seeks by keyframe and is fast; for audio-only streams that is
    accurate enough, and a stream copy cannot cut mid-frame anyway.
    """
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-ss", str(start), "-to", str(end),
           "-i", str(src), "-vn"]
    cmd += ["-c:a", "flac"] if reencode else ["-c", "copy"]
    cmd += [str(dest)]
    return subprocess.run(cmd, capture_output=True, text=True).returncode == 0


def tag(path: Path, meta: dict, track: dict, total: int) -> None:
    """Embed tags. Plex MUSIC libraries read these, not the NFO."""
    try:
        from mutagen import File as MFile
    except ImportError:
        return
    au = MFile(str(path), easy=False)
    if au is None:
        return
    song, guest = guest_of(track["title"])

    # THE TWO FIELDS DO DIFFERENT JOBS, and conflating them is what splits an
    # album in Plex.
    #
    #   ALBUMARTIST  what the album is FILED under. Must be identical on every
    #                track or Plex falls back to track artist and every distinct
    #                value becomes a separate album with the same name.
    #   ARTIST       who actually played THIS track. Free to vary.
    #
    # So the guest goes in ARTIST, where it is true, and never in ALBUMARTIST,
    # where it would fragment the record. This is the supported mechanism for a
    # multi-artist album — the same one compilations use with "Various Artists"
    # — not a workaround.
    track_artist = f"{meta['artist']} with {guest}" if guest else meta["artist"]
    vals = {
        "ARTIST": track_artist, "ALBUMARTIST": meta["artist"],
        "ALBUM": meta["album"], "TITLE": song,
        "TRACKNUMBER": str(track["position"]), "TRACKTOTAL": str(total),
        "DATE": meta["date"] or "", "VENUE": meta["venue"] or "",
        "CITY": meta.get("city") or "",
        "MEDIATYPE": "Live recording",
        # setlist.fm returns a tour name too, so this is the field an enrichment
        # pass fills in later when it was not known at rip time.
        "TOUR": meta.get("tour") or "",
        "CONTENTGROUP": meta.get("tour") or "",
        "COMMENT": meta["provenance"],
        "SOURCE_TYPE": meta.get("source_type") or "",
        # PER-TRACK guest only. This deliberately does NOT fall back to the
        # album-level guest from the title: "Band B with Guest Artist"
        # describes the SHOW, and Guest Artist joined for the last five songs. Falling
        # back tagged him onto "Opening Song", which he did not play on — a
        # confident, silent falsehood of exactly the kind nothing downstream
        # ever re-checks. The show-level billing lives in ALBUMGUEST.
        "GUEST": guest or "",
        "ALBUMGUEST": meta.get("guests") or "",
    }
    vals = {k: v for k, v in vals.items() if v}
    try:
        if path.suffix.lower() == ".flac":
            for k, v in vals.items():
                au[k.lower()] = v
        else:
            # MP4/M4A uses its own atom names; freeform for anything non-standard.
            from mutagen.mp4 import MP4, MP4FreeForm
            if isinstance(au, MP4):
                std = {"ARTIST": "\xa9ART", "ALBUMARTIST": "aART", "ALBUM": "\xa9alb",
                       "TITLE": "\xa9nam", "DATE": "\xa9day", "COMMENT": "\xa9cmt"}
                for k, v in vals.items():
                    if k in std:
                        au[std[k]] = [v]
                    elif k not in ("TRACKNUMBER", "TRACKTOTAL"):
                        au[f"----:com.apple.iTunes:{k}"] = [
                            MP4FreeForm(v.encode("utf-8"))]
                au["trkn"] = [(track["position"], total)]
        au.save()
    except Exception:  # noqa: BLE001
        pass


def write_nfo(dest_dir: Path, meta: dict, tracks: list[dict]) -> Path:
    """An album.nfo beside the tracks.

    NOTE: Plex MUSIC libraries do NOT read NFO — they read the embedded tags
    written above. This file is written because it was asked for and because it
    is the only human-readable record of where the album came from that survives
    outside the database, but it is documentation, not what Plex matches on.
    """
    import xml.etree.ElementTree as ET
    root = ET.Element("album")

    def sub(tag_, text):
        if text not in (None, ""):
            ET.SubElement(root, tag_).text = str(text)

    sub("title", meta["album"])
    sub("artist", meta["artist"])
    sub("albumartist", meta["artist"])
    sub("year", (meta["date"] or "")[:4])
    sub("releasedate", meta["date"])
    sub("genre", "Live")
    sub("venue", meta["venue"])
    sub("tour", meta.get("tour"))
    sub("plot", meta["provenance"])
    if meta.get("guests"):
        sub("guest", meta["guests"])
    for t in tracks:
        song, guest = guest_of(t["title"])
        tr = ET.SubElement(root, "track")
        ET.SubElement(tr, "position").text = str(t["position"])
        ET.SubElement(tr, "title").text = song
        ET.SubElement(tr, "duration").text = hms(t["seconds"])
        if guest:
            ET.SubElement(tr, "guest").text = guest
    sub("source", meta["url"])
    dest = dest_dir / "album.nfo"
    ET.ElementTree(root).write(dest, encoding="utf-8", xml_declaration=True)
    return dest


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--artist"); ap.add_argument("--date")
    ap.add_argument("--venue"); ap.add_argument("--city")
    ap.add_argument("--album", help="override the album folder name")
    ap.add_argument("--tour", help="tour name, e.g. 'Dissonance Tour'")
    ap.add_argument("--source-type", dest="source_type",
                    choices=["AUD", "SBD", "MATRIX", "FM", "WEB", "UNKNOWN"],
                    help="how it was recorded, if you know")
    ap.add_argument("--out", default=str(LIVE_AUDIO_DIR))
    ap.add_argument("--flac", action="store_true",
                    help="transcode to FLAC (lossless container, lossy source)")
    ap.add_argument("--apply", action="store_true", help="actually rip it")
    ap.add_argument("--no-playlist", action="store_true",
                    help="skip the Plex playlist that makes guest artists "
                         "searchable (see plex_playlist for why it is needed)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    info = probe(args.url)
    duration = int(info.get("duration") or 0)
    title = info.get("title") or ""
    guessed = parse_title(title)

    # yt-dlp first — it is right more often than a bespoke parser and free to
    # check — then the description, which is the common case for typed setlists.
    chapters = info.get("chapters") or []
    if chapters:
        tracks, src = [], "yt-dlp chapters"
        for i, c in enumerate(chapters):
            end = int(c.get("end_time") or (chapters[i + 1]["start_time"]
                                            if i + 1 < len(chapters) else duration))
            tracks.append({"position": i + 1, "title": clean_title(c.get("title", "")),
                           "start": int(c["start_time"]), "end": end,
                           "seconds": end - int(c["start_time"]), "raw": ""})
    else:
        tracks = parse_timestamps(info.get("description") or "", duration)
        src = "description timestamps"

    artist = args.artist or guessed["artist"]
    date = args.date or guessed["date"]
    venue = args.venue or guessed["venue"]
    # Source type stays OUT of the album title. It lives in the SOURCE_TYPE tag,
    # where it is searchable and does not clutter every album name in a music
    # library — "[AUD]" means something to a taper comparing sources and nothing
    # at all when you are just browsing for a show.
    #
    # It is appended ONLY to break a genuine collision: a second source of the
    # SAME artist, date and venue would otherwise land in the same folder and
    # overwrite the first. Collectors keep an audience recording and a matrix of
    # the same night deliberately, so those must stay distinct — but only then.
    # A named bootleg keeps its name; an unnamed show is filed by date+venue.
    # The date is not lost either way — it is in the DATE tag and drives the
    # year Plex sorts on.
    album = args.album or guessed.get("release") or " - ".join(
        x for x in (date, _safe(venue or "")) if x)
    if not args.album and args.source_type:
        probe_dir = Path(args.out) / _safe(artist or "") / _safe(album)
        if probe_dir.exists() and not _same_source(probe_dir, args.source_type):
            album += f" [{args.source_type}]"
    meta = {
        "artist": artist, "date": date, "venue": venue, "city": args.city,
        "album": album or _safe(title), "guests": guessed["guests"],
        "source_type": args.source_type, "url": args.url,
        "tour": args.tour,
        "provenance": (f"Ripped from {args.url}"
                       + (f" (uploaded by {info.get('uploader')})" if info.get("uploader") else "")
                       + f". Track index from {src}."),
    }

    if args.json:
        print(json.dumps({"meta": meta, "tracks": tracks, "guessed": guessed,
                          "duration": duration, "track_source": src}, indent=2))
        return 0

    print(f"\n{'=' * 74}\n  {title}\n{'=' * 74}")
    print(f"  duration   : {hms(duration)}    uploaded: {info.get('upload_date')}")
    print(f"  artist     : {artist or '?'}")
    print(f"  date       : {date or '?'}" + (
        f"   <- from the TITLE, not the {info.get('upload_date')} upload"
        if date and date[:4] != str(info.get("upload_date") or "")[:4] else ""))
    print(f"  venue      : {venue or '?'}")
    if guessed["guests"]:
        print(f"  guests     : {guessed['guests']}")
    if args.tour:
        print(f"  tour       : {args.tour}")
    print(f"  album      : {meta['album']}"
          + ("   <- the recording's own name, from the title"
             if guessed.get("release") and not args.album else ""))
    print(f"  tracks from: {src}")

    if not tracks:
        print("\n  NO TIMESTAMPS FOUND — nothing to split on.")
        print("  This tool only helps when the uploader typed a track index.")
        return 1

    covered = sum(t["seconds"] for t in tracks)
    print(f"\n  {len(tracks)} tracks, covering {hms(covered)} of {hms(duration)}"
          f" ({covered * 100 // max(1, duration)}%)\n")
    for t in tracks:
        song, guest = guest_of(t["title"])
        g = f"  (with {guest})" if guest else ""
        print(f"    {t['position']:>2}. {hms(t['start']):>7}  {song[:44]:<44}"
              f" {hms(t['seconds']):>6}{g}")

    if not artist or not date:
        print("\n  MISSING " + " and ".join(
            x for x, v in (("artist", artist), ("date", date)) if not v).upper()
            + " — pass --artist / --date. Refusing to guess.")
        if not args.apply:
            return 1
        return 1

    dest_dir = Path(args.out) / _safe(artist) / _safe(meta["album"])
    print(f"\n  -> {dest_dir}")
    if not args.apply:
        print("\n  DRY RUN. Add --apply to rip it.")
        return 0

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        print("\n  downloading audio…")
        source = download_audio(args.url, work, args.flac)
        ext = ".flac" if args.flac else source.suffix
        print(f"  got {source.name} ({source.stat().st_size // 1048576} MB)"
              + ("" if args.flac else "  — splitting with a stream copy, "
                                      "no re-encode"))
        dest_dir.mkdir(parents=True, exist_ok=True)
        written = 0
        for t in tracks:
            song, _ = guest_of(t["title"])
            out = dest_dir / f"{t['position']:02d} - {_safe(song)}{ext}"
            if split_track(source, out, t["start"], t["end"], args.flac):
                tag(out, meta, t, len(tracks))
                written += 1
                print(f"    {out.name}")
            else:
                print(f"    FAILED: {song}")
        cover = fetch_cover(args.url, work)
        if cover:
            # cover.jpg is what Plex's music scanner looks for; poster.jpg is
            # the generic Plex poster name. Writing both costs a few KB and
            # removes a whole class of "why is there no artwork" question.
            for name in ("cover.jpg", "poster.jpg"):
                shutil.copy2(cover, dest_dir / name)
            for f in dest_dir.glob(f"*{ext}"):
                embed_cover(f, cover)
            print(f"    cover.jpg + poster.jpg ({cover.stat().st_size // 1024} KB, "
                  "also embedded in every track)")
        else:
            print("    no thumbnail available — album will show a placeholder")
        nfo = write_nfo(dest_dir, meta, tracks)
        print(f"\n  wrote {written}/{len(tracks)} tracks + {nfo.name}")
        if not args.no_playlist:
            plex_playlist(meta, tracks)
        print("  (Plex music libraries match on the EMBEDDED tags; the .nfo is "
              "a human-readable record.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
