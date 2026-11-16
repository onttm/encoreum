#!/usr/bin/env python3
"""
ingest-taper.py
=========================================================================
Read a taper archive (folder or zip) and work out what show it is.

WHY THIS EXISTS
    Bands with a taping policy publish to structured archives — the Live Music
    Archive has 303,195 items with per-file title/track/length/md5, and phish.in
    exposes set structure and tour names. Those need no parsing at all.

    Bands WITHOUT a taping policy do not. Band C, Dave Matthews Band, Band D
    and Tower of Power all return ZERO results on LMA. Their recordings live in
    private trackers and fan communities, and arrive as a folder of FLACs with
    a text file. That is the case this handles, and it is the only case that
    cannot be handled any other way.

    Every community differs — etree, the DMB circle, The Jam Ports, the Band D forums — but they all converge on the same artifact, so ONE parser with
    dialect tolerance beats one scraper per site.

WHAT IT REFUSES TO DO
    Guess. Where the evidence is ambiguous it says so and stops. Verified
    against a real archive where 11 audio files sat against 12 setlist lines and
    the last entry was PA music: the mapping genuinely cannot be resolved from
    the archive alone, and silently tagging it would have produced an album with
    the wrong song titles and nothing flagged.

USAGE
    ./ingest-taper.py <folder-or-zip>            # inspect, change nothing
    ./ingest-taper.py <path> --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

AUDIO_EXT = {".flac", ".shn", ".mp3", ".m4a", ".ogg", ".wav", ".ape"}
INFO_EXT = {".txt", ".nfo", ".md"}

# Order matters: a matrix lineage names BOTH a board and mics, so it must be
# tested first or it gets filed as whichever pattern happens to match earlier.
SOURCE_RULES = [
    ("MATRIX", r"\bmatrix\b|\bmtx\b|\bsbd\b.{0,40}\+.{0,40}\baud\b|\baud\b.{0,40}\+.{0,40}\bsbd\b"),
    ("FM", r"\bfm\b|broadcast|tuner|\bradio\b"),
    ("SBD", r"\bsbd\b|soundboard|\bboard\b|\bdsbd\b|\bald\b"),
    ("AUD", r"\bfob\b|\baud\b|schoeps|nakamichi|\bnak\b|core ?sound|avantone|neumann|"
            r"microphone|\bmics?\b|cardioid|\bortf\b|\bcmc\d|\bmk4\b|\bck-?\d|ecm\d|"
            r"\bdpa\b|akg|sennheiser|\bat\d{3,}"),
]

# Entries that appear in a setlist but are NOT performed songs. setlist.fm calls
# these "tape" and excludes them; taper info files annotate them inline.
NOT_A_SONG = re.compile(
    r"\[(?:[^\]]*\b(?:over pa|on pa|pa|tape|intro|outro|walk[- ]?(?:in|out))\b[^\]]*)\]"
    r"|^\s*(?:tuning|crowd|banter|intro|outro|applause|encore break|set break|"
    r"band intros?|announcements?)\s*$", re.I)

FIELD_ALIASES = {
    "source": ("source", "src", "recording", "full lineage"),
    "lineage": ("lineage", "transfer", "conversion", "full lineage", "xfer"),
    "taper": ("taper", "tapers", "taper(s)", "recorded by", "taped by"),
    "location": ("location", "position", "taping location"),
    "notes": ("notes", "show notes", "comments"),
    "media": ("media type", "format", "media"),
}

MONTHS = ("january february march april may june july august september october "
          "november december").split()


def read_text(p: Path) -> str:
    """Decode an info file. NEVER assume UTF-8.

    Real archives are CP1252 far more often than not — a Band C info file used
    0xC6 for the 'AE' ligature in a song title, which fails a strict UTF-8
    decode outright and, decoded carelessly, writes a replacement character into
    the tag. Since setlist.fm matches titles against MusicBrainz, a mangled
    title never matches and the track ends up separated from its studio
    counterpart in the library.
    """
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace")


def detect_source_type(text: str) -> str:
    t = (text or "").lower()
    for name, pat in SOURCE_RULES:
        if re.search(pat, t):
            return name
    return "UNKNOWN"


def parse_date(s: str) -> str | None:
    """Accept the formats archives actually use, not just ISO."""
    s = s.strip()
    m = re.search(r"\b(19|20)(\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})\b", s)
    if m:
        y, mo, d = int(m.group(1) + m.group(2)), int(m.group(3)), int(m.group(4))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.search(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/]((?:19|20)\d{2})\b", s)
    if m:                                     # US m/d/y
        mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.search(rf"\b({'|'.join(MONTHS)})[a-z]*\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+"
                  r"((?:19|20)\d{2})\b", s, re.I)
    if m:
        mo = MONTHS.index(m.group(1).lower()) + 1
        return f"{int(m.group(3)):04d}-{mo:02d}-{int(m.group(2)):02d}"
    return None


def parse_tracklist(text: str) -> list[dict]:
    """Tracks, with durations when the info file provides them.

    A dropped track is the dangerous failure here, so the separator between
    title and duration is deliberately permissive: one real archive aligned its
    columns with tabs, and a single long title left exactly ONE tab where every
    other line had two or three. Requiring two dropped that track silently, and
    only the running total revealed it.
    """
    out = []
    numbered = re.compile(
        r"^\s*(\d{1,3})[.)\s]\s*(.+?)"
        r"(?:\s+(\d{1,2}):(\d{2})(?:[.:](\d{2}))?)?\s*$")
    for line in text.splitlines():
        if not line.strip() or re.match(r"^\s*(total|disc|set)\b", line, re.I):
            continue
        m = numbered.match(line)
        if not m:
            continue
        num, title = int(m.group(1)), m.group(2).strip()
        if not title or len(title) > 160:
            continue
        dur = None
        if m.group(3):
            dur = int(m.group(3)) * 60 + int(m.group(4))
            if m.group(5):                 # frames (MM:SS.FF at 75fps)
                dur += int(m.group(5)) / 75
        segue = bool(re.search(r"[>→]\s*$", title))
        out.append({
            "position": num,
            "title": re.sub(r"\s*[>→]\s*$", "", title).strip(),
            "claimed_seconds": dur,
            "segue_into_next": segue,
            "performed": not bool(NOT_A_SONG.search(title)),
            "raw": line.rstrip(),
        })
    return out


def parse_unnumbered_setlist(text: str) -> list[dict]:
    """Fallback: a bare 'Setlist:' block with one song per line, no numbers."""
    m = re.search(r"^\s*set\s?list\s*:?\s*$(.*)", text, re.I | re.M | re.S)
    if not m:
        return []
    out = []
    # Position is assigned from ACCEPTED entries, not from enumerate() over raw
    # lines. The regex's `$(.*)` leaves a leading empty line in the captured
    # block, so counting raw lines started every track at 2 — which would have
    # written wrong track numbers into every tag.
    for line in m.group(1).splitlines():
        t = line.strip()
        if not t or re.match(r"^(notes?|compiled|total|source|taper)\b", t, re.I):
            if out:
                break
            continue
        out.append({"position": len(out) + 1,
                    "title": re.sub(r"\s*[>→]\s*$", "", t).strip(),
                    "claimed_seconds": None,
                    "segue_into_next": bool(re.search(r"[>→]\s*$", t)),
                    "performed": not bool(NOT_A_SONG.search(t)), "raw": t})
    return out


def parse_info(text: str) -> dict:
    """Pull the header and fields out of an info file of unknown dialect."""
    lines = [l.rstrip() for l in text.splitlines()]
    info: dict = {"fields": {}}

    for line in lines:
        m = re.match(r"^\s*([A-Za-z][A-Za-z ()/]{1,28}?)\s*:\s*(.+?)\s*$", line)
        if not m:
            continue
        label, value = m.group(1).strip().lower(), m.group(2).strip()
        for canon, aliases in FIELD_ALIASES.items():
            if label in aliases:
                info["fields"].setdefault(canon, value)
    if "lineage" in info["fields"] and "source" in info["fields"]:
        combined = f"{info['fields']['source']} > {info['fields']['lineage']}"
    else:
        combined = info["fields"].get("source") or info["fields"].get("lineage") or ""
    info["lineage_full"] = combined
    info["source_type"] = detect_source_type(combined or text[:600])

    # Header block: the first few non-empty lines before any Label: line.
    header = []
    for line in lines:
        if not line.strip():
            if header:
                break
            continue
        if re.match(r"^\s*[A-Za-z][A-Za-z ()/]{1,28}?\s*:", line):
            break
        header.append(line.strip())
        if len(header) >= 5:
            break
    info["header"] = header

    # Line ORDER varies between communities: one archive is artist/date/city/
    # venue, another is artist/venue/city/date. So classify each line by what it
    # looks like rather than by position.
    date = None
    rest = []
    for h in header:
        d = parse_date(h)
        if d and not date:
            date = d
            continue
        rest.append(h)
    info["date"] = date
    info["artist"] = rest[0] if rest else None
    loc = [r for r in rest[1:]]
    info["venue"] = None
    info["city"] = None
    for r in loc:
        if re.search(r",\s*[A-Z]{2}\b|,\s*(USA|UK|Canada)\b", r) and not info["city"]:
            info["city"] = r
        elif not info["venue"]:
            info["venue"] = r
    if not info["venue"] and loc:
        info["venue"] = loc[0]

    tracks = parse_tracklist(text) or parse_unnumbered_setlist(text)
    info["tracks"] = tracks
    return info


def read_existing_tags(audio: list[Path]) -> dict:
    """Metadata the taper already embedded. A whole tier I initially ignored.

    Tapers frequently tag their own releases, and those tags are often BETTER
    than anything in an info file — a Tower of Power archive carried artist,
    date, venue, taper, source and conversion in every FLAC, and the audio was
    the ONLY place that survived being separated from its .txt.

    Precedence is info file > embedded tags > filenames: the info file is the
    community's canonical description, but embedded tags beat guessing.
    """
    out: dict = {}
    if not audio:
        return out
    try:
        from mutagen import File as MFile
    except ImportError:
        return out
    for f in audio[: min(3, len(audio))]:
        try:
            au = MFile(str(f))
        except Exception:  # noqa: BLE001
            continue
        if not au or not getattr(au, "tags", None):
            continue
        g = lambda k: (au.get(k) or [None])[0] if hasattr(au, "get") else None
        for key, tag in (("artist", "artist"), ("venue", "venue"),
                         ("taper", "taper")):
            v = g(tag)
            if v and not out.get(key):
                out[key] = str(v).strip()
        d = g("date")
        if d and not out.get("date"):
            out["date"] = parse_date(str(d)) or str(d)[:10]
        # Lineage may be split across several taper-chosen tag names.
        chunks = [str(g(k)).strip() for k in ("source", "lineage", "conversion",
                                              "transfer") if g(k)]
        if chunks and not out.get("lineage_full"):
            # Tapers often embed the label too ("Source: ..."); strip it so the
            # value is not doubly prefixed once we re-tag.
            cleaned = [re.sub(r"^\s*(source|lineage|conversion|transfer)\s*:\s*", "",
                              c, flags=re.I) for c in chunks]
            out["lineage_full"] = " > ".join(cleaned)
        if out.get("artist") and out.get("date") and out.get("lineage_full"):
            break
    return out


def tracks_from_tags(audio: list[Path]) -> list[dict]:
    """Build a tracklist from per-file title tags, when the taper set them.

    If every file carries a title, the archive already contains its own setlist
    and no human input is needed. Falls back silently when titles are missing or
    incomplete — a partial tracklist is worse than none, because it looks
    authoritative while quietly mislabelling half the album.
    """
    try:
        from mutagen import File as MFile
    except ImportError:
        return []
    rows = []
    for f in audio:
        try:
            au = MFile(str(f))
        except Exception:  # noqa: BLE001
            return []
        if not au or not getattr(au, "tags", None):
            return []
        title = (au.get("title") or [None])[0]
        if not title:
            return []
        num = (au.get("tracknumber") or [None])[0]
        try:
            pos = int(str(num).split("/")[0])
        except (TypeError, ValueError):
            pos = len(rows) + 1
        t = str(title).strip()
        rows.append({"position": pos,
                     "title": re.sub(r"\s*[>\u2192]\s*$", "", t).strip(),
                     "claimed_seconds": None,
                     "segue_into_next": bool(re.search(r"[>\u2192]\s*$", t)),
                     "performed": not bool(NOT_A_SONG.search(t)),
                     "raw": t})
    rows.sort(key=lambda r: r["position"])
    return rows


def guess_from_filenames(audio: list[Path]) -> dict:
    """Last-resort metadata from the filenames themselves.

    Taper filenames are conventional enough to be worth reading: gd91-03-17d1t02,
    tool1998-08-30.v1.t01, top2012-07-21t05. They reliably carry a DATE and an
    artist abbreviation. The abbreviation is NOT expanded into an artist name —
    `top` is Tower of Power, not "the top of something", and guessing wrong is
    worse than leaving it blank for a human to fill in.
    """
    out: dict = {}
    for f in audio:
        n = f.name
        m = re.search(r"((?:19|20)\d{2})[-.]?(\d{2})[-.]?(\d{2})", n)
        if m:
            y, mo, d = m.groups()
            if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
                out["date"] = f"{y}-{mo}-{d}"
                pre = n[:m.start()].strip(" -._")
                if pre and len(pre) <= 12:
                    out["artist_hint"] = pre
                break
    return out


def parse_supplied_setlist(text: str) -> list[dict]:
    """A setlist typed or pasted by a person. Works for ANY archive.

    This is the universal path: the audio gives count and order, this gives the
    titles. It accepts a bare list, a numbered list, or setlist.fm-style lines,
    and honours the same annotations the info-file parser does — so `[over PA]`
    or a bare `Tuning` line is still excluded from the track mapping.
    """
    rows = parse_tracklist(text)
    if rows:
        return rows
    out = []
    for line in text.splitlines():
        t = line.strip()
        if not t or t.startswith("#"):
            continue
        t = re.sub(r"^\s*\d{1,3}[.)]\s*", "", t)          # strip any numbering
        out.append({"position": len(out) + 1,
                    "title": re.sub(r"\s*[>\u2192]\s*$", "", t).strip(),
                    "claimed_seconds": None,
                    "segue_into_next": bool(re.search(r"[>\u2192]\s*$", t)),
                    "performed": not bool(NOT_A_SONG.search(t)),
                    "raw": t})
    return out


def audio_durations(files: list[Path]) -> dict[str, float]:
    out = {}
    for f in files:
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(f)],
                capture_output=True, text=True, timeout=60)
            out[f.name] = float(r.stdout.strip())
        except (OSError, subprocess.SubprocessError, ValueError):
            out[f.name] = 0.0
    return out


def collect(path: Path, workdir: Path) -> tuple[list[Path], list[Path]]:
    """Return (audio files, info files), extracting a zip if needed.

    Only the small text files are extracted from an archive — the audio is left
    compressed until the mapping has actually been confirmed.
    """
    if path.is_dir():
        files = sorted(p for p in path.rglob("*") if p.is_file())
        return ([f for f in files if f.suffix.lower() in AUDIO_EXT],
                [f for f in files if f.suffix.lower() in INFO_EXT])
    if zipfile.is_zipfile(path):
        infos = []
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            for n in names:
                if Path(n).suffix.lower() in INFO_EXT:
                    z.extract(n, workdir)
                    infos.append(workdir / n)
        audio = [Path(n) for n in names if Path(n).suffix.lower() in AUDIO_EXT]
        return sorted(audio), sorted(infos)
    return [], []


def align(tracks: list[dict], durations: dict[str, float],
          audio: list[Path]) -> dict:
    """Match songs to audio files, and say how confident that is.

    Three outcomes, because they need different handling:
      exact      counts match and every claimed duration agrees -> safe to tag
      probable   counts match, no durations to check against
      ambiguous  counts disagree -> refuse, and show the conflict
    """
    performed = [t for t in tracks if t["performed"]]
    excluded = [t for t in tracks if not t["performed"]]
    n_audio, n_perf = len(audio), len(performed)

    res = {"audio_files": n_audio, "setlist_entries": len(tracks),
           "performed": n_perf, "excluded_as_non_song": [t["title"] for t in excluded]}

    if n_audio != n_perf:
        res.update(confidence="ambiguous", mapping=[],
                   reason=(f"{n_audio} audio file(s) but {n_perf} performed song(s). "
                           "The archive cannot resolve this — confirm by hand."))
        return res

    have_dur = [t for t in performed if t["claimed_seconds"]]
    mapping, mismatches = [], []
    for t, f in zip(performed, audio):
        actual = durations.get(f.name)
        delta = (actual - t["claimed_seconds"]
                 if (actual and t["claimed_seconds"]) else None)
        if delta is not None and abs(delta) > 2:
            mismatches.append((t["title"], round(delta, 1)))
        mapping.append({"file": f.name, "position": t["position"],
                        "title": t["title"], "claimed": t["claimed_seconds"],
                        "actual": round(actual, 1) if actual else None,
                        "delta": round(delta, 1) if delta is not None else None,
                        "segue_into_next": t["segue_into_next"]})
    if mismatches:
        res.update(confidence="ambiguous", mapping=mapping,
                   reason=f"duration mismatch on {len(mismatches)} track(s): {mismatches[:3]}")
    elif have_dur:
        res.update(confidence="exact", mapping=mapping,
                   reason=f"{len(have_dur)} track duration(s) verified against the audio")
    else:
        res.update(confidence="probable", mapping=mapping,
                   reason="counts match, but the info file gives no durations to verify against")
    return res


def album_title(r: dict) -> str:
    """Album name for the Live Music library.

    Deliberately leads with the date: a live library sorts by WHEN, and an
    artist with forty shows is unusable sorted by venue name.
    """
    bits = [r.get("date") or "unknown date"]
    v = r.get("venue")
    c = r.get("city")
    if v:
        bits.append(v)
    if c and c != v:
        bits.append(c)
    label = " - ".join(bits)
    st = r.get("source_type")
    return f"{label} [{st}]" if st and st != "UNKNOWN" else label


def write_tags(r: dict, root: Path, src_root: Path, dry: bool = True) -> dict:
    """Write Vorbis comments and lay the album out for Plex.

    Plex MUSIC libraries do NOT read NFO sidecars — they read embedded tags. So
    everything the video side would put in an NFO has to go into the file
    itself, which also means it travels with the file if it is ever moved.
    """
    try:
        from mutagen.flac import FLAC
        from mutagen.oggvorbis import OggVorbis
    except ImportError:
        return {"ok": False, "error": "mutagen not installed (pip install mutagen)"}

    a = r["alignment"]
    if not a["mapping"]:
        return {"ok": False, "error": "no confirmed track mapping"}

    artist = r.get("artist") or "Unknown Artist"
    album = album_title(r)
    dest = root / _safe(artist) / _safe(album)
    plan, written = [], 0
    for m in a["mapping"]:
        src = src_root / m["file"]
        ext = src.suffix.lower()
        target = dest / f"{m['position']:02d} - {_safe(m['title'])}{ext}"
        plan.append({"from": m["file"], "to": str(target.relative_to(root))})
        if dry:
            continue
        dest.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy2(src, target)
        if ext == ".flac":
            au = FLAC(str(target))
        elif ext == ".ogg":
            au = OggVorbis(str(target))
        else:
            written += 1          # copied, but this format is not tagged here
            continue
        au["ARTIST"] = artist
        au["ALBUMARTIST"] = artist
        au["ALBUM"] = album
        au["TITLE"] = m["title"]
        au["TRACKNUMBER"] = str(m["position"])
        au["TRACKTOTAL"] = str(len(a["mapping"]))
        if r.get("date"):
            au["DATE"] = r["date"]
        if r.get("venue"):
            au["VENUE"] = r["venue"]
        if r.get("city"):
            # NOT "LOCATION": tapers use that for the recording POSITION
            # ("Section 3, Row K, Seats 11-12"), which is provenance worth
            # keeping. Overwriting it with a city would destroy it.
            au["CITY"] = r["city"]
        if r.get("source_type"):
            au["SOURCE_TYPE"] = r["source_type"]     # AUD / SBD / MATRIX / FM
        if r.get("lineage"):
            au["LINEAGE"] = r["lineage"]
        if r.get("taper"):
            au["TAPER"] = r["taper"]
        au["MEDIATYPE"] = "Live recording"
        if m.get("segue_into_next"):
            # Segues matter: the track runs continuously into the next one, so a
            # gapless player should not insert a break.
            au["SEGUE"] = "1"
        if r.get("cross_link"):
            au["COMMENT"] = r["cross_link"]
        au.save()
        written += 1
    return {"ok": True, "dry_run": dry, "album": str(dest), "written": written,
            "plan": plan}


def _safe(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]', " ", s or "").strip()
    return re.sub(r"\s{2,}", " ", s)[:120].rstrip(" .")


def inspect(path: Path, supplied_setlist: str = "", cross_link: str = "") -> dict:
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        audio, infos = collect(path, work)
        if not audio:
            return {"path": str(path), "error": "no audio files found"}
        if not infos:
            # An archive with no info file is still ingestable. The AUDIO is the
            # ground truth for how many tracks there are and what order they run
            # in; only the TITLES are missing, and a supplied setlist provides
            # those. Refusing here would have made the tool useless for exactly
            # the sources that need it most.
            best = {"_file": "(none)", "_score": 0, "fields": {}, "tracks": [],
                    "header": [], "lineage_full": "", "source_type": "UNKNOWN"}
            best.update(guess_from_filenames(audio))
            parsed = [best]
        else:
            parsed = []

        # Prefer the richest info file rather than the first: one archive shipped
        # both an 'info.txt' with no setlist and an 'orig.txt' with one, plus a
        # fuller venue name.
        for f in infos:
            p = parse_info(read_text(f))
            p["_file"] = f.name
            p["_score"] = (len(p["tracks"]) * 10 + len(p["fields"]) * 2
                           + (5 if p.get("date") else 0))
            parsed.append(p)
        parsed.sort(key=lambda p: -p["_score"])
        best = parsed[0]

        # info file > embedded tags > filenames
        embedded = read_existing_tags(audio if path.is_dir() else [])
        for k, v in embedded.items():
            if not best.get(k):
                best[k] = v
        if embedded.get("lineage_full") and not best.get("lineage_full"):
            best["lineage_full"] = embedded["lineage_full"]
        if best.get("source_type") in (None, "UNKNOWN") and best.get("lineage_full"):
            best["source_type"] = detect_source_type(best["lineage_full"])

        if not best.get("artist") or not best.get("date"):
            for k, v in guess_from_filenames(audio).items():
                best.setdefault(k, None)
                if not best.get(k):
                    best[k] = v

        # Merge anything the runners-up know that the winner does not.
        for other in parsed[1:]:
            for k in ("date", "artist", "venue", "city"):
                if not best.get(k) and other.get(k):
                    best[k] = other[k]
            for k, v in other["fields"].items():
                best["fields"].setdefault(k, v)
            if not best["tracks"] and other["tracks"]:
                best["tracks"] = other["tracks"]

        # A supplied setlist WINS. It is the whole point of the manual path:
        # any archive, from any source, becomes ingestable the moment a person
        # provides the song titles. The audio still decides count and order.
        supplied = parse_supplied_setlist(supplied_setlist) if supplied_setlist else []
        from_tags = []
        if supplied:
            best["tracks"] = supplied
        elif not best["tracks"] and path.is_dir():
            from_tags = tracks_from_tags(audio)
            if from_tags:
                best["tracks"] = from_tags

        durations = audio_durations(audio) if path.is_dir() else {}
        result = {
            "path": str(path),
            "container": "folder" if path.is_dir() else "zip",
            "info_files": [p["_file"] for p in parsed],
            "info_file_used": best["_file"],
            "artist": best.get("artist"),
            "date": best.get("date"),
            "venue": best.get("venue"),
            "city": best.get("city"),
            "source_type": best.get("source_type"),
            "lineage": best.get("lineage_full"),
            "taper": best["fields"].get("taper"),
            "taper_location": best["fields"].get("location"),
            "notes": best["fields"].get("notes"),
            "tracks": best["tracks"],
            "setlist_source": ("supplied by hand" if supplied
                               else ("embedded track tags" if from_tags
                                     else ("info file" if best["tracks"] else "none"))),
            "artist_hint": best.get("artist_hint"),
            "cross_link": cross_link or None,
        }
        result["alignment"] = align(best["tracks"], durations, audio)
        return result


def render(r: dict) -> None:
    if r.get("error"):
        print(f"  {r['path']}\n    ERROR: {r['error']}")
        return
    print(f"\n{'=' * 74}")
    print(f"  {r.get('artist') or '(unknown artist)'} — {r.get('date') or '(no date)'}")
    print(f"{'=' * 74}")
    print(f"  venue      : {r.get('venue') or '?'}")
    print(f"  city       : {r.get('city') or '?'}")
    print(f"  source     : {r.get('source_type')}")
    print(f"  lineage    : {(r.get('lineage') or '?')[:64]}")
    if r.get("taper"):
        print(f"  taper      : {r['taper']}")
    print(f"  container  : {r['container']}  info: {r['info_file_used']}"
          + (f"  (also saw {len(r['info_files']) - 1} other)" if len(r["info_files"]) > 1 else ""))

    a = r["alignment"]
    print(f"\n  ALIGNMENT: {a['confidence'].upper()}")
    print(f"    {a['reason']}")
    if a["excluded_as_non_song"]:
        print(f"    excluded as non-song: {a['excluded_as_non_song']}")
    if a["mapping"]:
        print()
        for m in a["mapping"]:
            d = f"{m['delta']:+.1f}s" if m["delta"] is not None else "  —  "
            seg = " >" if m["segue_into_next"] else ""
            print(f"    {m['position']:>2}. {m['title'][:38]:<38}{seg:<2} {d:>7}  {m['file'][:28]}")
    if r.get("setlist_source"):
        print(f"\n  setlist from: {r['setlist_source']}")
    if a["confidence"] == "ambiguous" and not r.get("write", {}).get("ok"):
        print("\n    -> NOT SAFE TO TAG. Supply --setlist, or pass --force.")
    w = r.get("write")
    if w:
        if not w.get("ok"):
            print(f"\n  WRITE: {w.get('error')}")
        else:
            print(f"\n  {'WOULD WRITE' if w['dry_run'] else 'WROTE'}: {w['album']}")
            for x in w["plan"][:4]:
                print(f"      {x['to']}")
            if len(w["plan"]) > 4:
                print(f"      … {len(w['plan']) - 4} more")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="taper folder(s) or zip(s)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--setlist", metavar="FILE",
                    help="setlist to use, one song per line ('-' for stdin). "
                         "Overrides whatever the archive claims. This is the "
                         "universal path: ANY archive becomes ingestable once a "
                         "person supplies the titles.")
    ap.add_argument("--artist", help="override the artist")
    ap.add_argument("--date", help="override the date (YYYY-MM-DD)")
    ap.add_argument("--venue", help="override the venue")
    ap.add_argument("--city", help="override the city")
    ap.add_argument("--source-type", dest="source_type",
                    choices=["AUD", "SBD", "MATRIX", "FM", "UNKNOWN"],
                    help="override the source type (AUD/SBD/MATRIX/FM)")
    ap.add_argument("--cross-link", default="",
                    help="pointer to the other format, e.g. "
                         "'Concert video in Concerts > Band C > Example Tour'")
    ap.add_argument("--out", metavar="DIR",
                    help="Live Music library root to write into")
    ap.add_argument("--apply", action="store_true",
                    help="actually copy and tag (default is a dry run)")
    ap.add_argument("--force", action="store_true",
                    help="tag even when the mapping is ambiguous — say why in a note")
    args = ap.parse_args()

    setlist_text = ""
    if args.setlist:
        setlist_text = (sys.stdin.read() if args.setlist == "-"
                        else read_text(Path(args.setlist)))

    results = []
    for p in args.paths:
        r = inspect(Path(p), setlist_text, args.cross_link)
        for k in ("artist", "date", "venue", "city", "source_type"):
            if getattr(args, k):
                r[k] = getattr(args, k)
        if args.out and not r.get("error"):
            conf = r["alignment"]["confidence"]
            if conf == "ambiguous" and not args.force:
                r["write"] = {"ok": False,
                              "error": "mapping is ambiguous — resolve it, supply "
                                       "--setlist, or pass --force"}
            else:
                src_root = Path(p) if Path(p).is_dir() else None
                if src_root is None:
                    r["write"] = {"ok": False,
                                  "error": "zip input: extract it first, then "
                                           "point --out at the extracted folder"}
                else:
                    r["write"] = write_tags(r, Path(args.out), src_root,
                                            dry=not args.apply)
        results.append(r)
    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        for r in results:
            render(r)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
