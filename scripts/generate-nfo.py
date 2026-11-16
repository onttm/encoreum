#!/usr/bin/env python3
"""
generate-nfo.py
=========================================================================
Build thorough, Plex/Kodi-compatible local-metadata files from the
.info.json that yt-dlp saved next to each concert video.

It writes:
    <show>/tvshow.nfo                     - the series
    <show>/Season NN/season.nfo           - each season
    <show>/Season NN/<video>.nfo          - one per episode (episodedetails)

Each episode NFO is complete: title, show title, season/episode, full plot,
aired/premiered date, year, studio, director, genres, tags, runtime,
episode thumbnail, the YouTube unique id, and the source URL.

------------------------------------------------------------------ IMPORTANT
Plex's built-in agents do NOT read .nfo files. To use these, install the free
**XBMCnfoTVImporter** agent and select it for the library (steps are in the
guide). Without it, Plex ignores NFO and you'd rely on filenames + embedded
tags instead.
---------------------------------------------------------------------------

USAGE
    python3 generate-nfo.py "/path/to/Concerts/Band A" --show "Band A"

Options:
    --show     NAME     series name (required)
    --genre    G        repeatable; default: Music, Concert
    --studio   NAME     override the studio/label (default: the channel name)
    --plot     TEXT     series-level description for tvshow.nfo
    --overwrite         rewrite NFO files that already exist (default: skip them)
    --add-credits       ALSO add the source-channel credit to NFOs that already
                        exist, editing only their plot/tags (safe to re-run)

SOURCE CREDIT
    Every episode plot ends with a "Video source:" line naming the channel and
    site it came from, with links, and tvshow.nfo lists every source channel.
    The plot is what Plex actually shows (it ignores <source> and most tags),
    so that is where the credit goes. Existing NFOs are normally skipped; with
    --add-credits they get the line appended in place without anything else
    being rewritten, so hand edits and later setlist credits survive.

Run it AFTER downloading (and after rename-by-footage-date.py if you use it).
It only reads .info.json and writes .nfo - it never touches your video files.
=========================================================================
"""

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

VIDEO_EXTS = {".mkv", ".mp4", ".webm", ".m4v", ".avi", ".mov"}
IMG_EXTS = [".jpg", ".jpeg", ".png", ".webp"]

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MN = "|".join(sorted(MONTHS, key=len, reverse=True))
DATE_PATTERNS = [
    (re.compile(r"\b(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})\b"),
     lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"\b(" + _MN + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})\b", re.I),
     lambda m: (int(m[3]), MONTHS[m[1].lower()], int(m[2]))),
    (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + _MN + r")\.?,?\s+(20\d{2})\b", re.I),
     lambda m: (int(m[3]), MONTHS[m[2].lower()], int(m[1]))),
]
CODE_RE = re.compile(r"[Ss](\d+)[Ee](\d+)")


def find_date(text):
    if not text:
        return None
    for regex, build in DATE_PATTERNS:
        for m in regex.finditer(text):
            try:
                y, mo, d = build(m)
                if 1 <= mo <= 12 and 1 <= d <= 31 and 1900 <= y <= 2100:
                    return date(y, mo, d)
            except (KeyError, ValueError):
                continue
    return None


def meta_date(info):
    for key in ("release_date", "upload_date"):
        v = str(info.get(key) or "")
        if len(v) == 8 and v.isdigit():
            try:
                return date(int(v[:4]), int(v[4:6]), int(v[6:8]))
            except ValueError:
                pass
    return None


def load_info(video):
    """Return the parsed .info.json dict for a video, or {}."""
    cand = video.parent / (video.name[: -len(video.suffix)] + ".info.json")
    if cand.exists():
        try:
            return json.loads(cand.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def find_thumb(video):
    base = video.name[: -len(video.suffix)]
    for ext in IMG_EXTS:
        p = video.parent / (base + ext)
        if p.exists():
            return p.name
    return None


def sub(parent, tag, text, **attrib):
    e = ET.SubElement(parent, tag, {k: v for k, v in attrib.items() if v is not None})
    if text is not None and text != "":
        e.text = str(text)
    return e


def write_xml(root, path, overwrite):
    if path.exists() and not overwrite:
        return False
    ET.indent(root, space="  ")
    xml = ET.tostring(root, encoding="unicode")
    path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n' + xml + "\n", encoding="utf-8")
    return True


def compute_episode_numbers(videos):
    """For files that carry NO SxxExx (the performance-name scheme), assign a
    UNIQUE episode number within each concert year, ordered by (date, filename).

    This is what keeps per-song playlists (many videos sharing one date) as
    separate Plex episodes: the on-disk name only has a 3-digit collision
    sequence, but the NFO needs a season-wide-unique <episode>. Returns
    {video_path: (season_year, episode_index)}.
    """
    from collections import defaultdict
    by_year = defaultdict(list)
    for v in videos:
        if CODE_RE.search(v.stem):
            continue  # already has an explicit SxxExx -> trust it
        info = load_info(v)
        aired = (find_date(info.get("title", "")) or find_date(info.get("description", ""))
                 or meta_date(info))
        if aired:
            by_year[aired.year].append((aired, v))
    out = {}
    for year, entries in by_year.items():
        for i, (_, v) in enumerate(sorted(entries, key=lambda e: (e[0], e[1].name)), start=1):
            out[v] = (year, i)
    return out


def season_episode(video, info, ep_override=None):
    """(season, episode, aired_date). Prefers the SxxExx already in the filename;
    then a caller-supplied unique per-year number; then the date's MMDD."""
    m = CODE_RE.search(video.stem)
    aired = find_date(info.get("title", "")) or find_date(info.get("description", "")) or meta_date(info)
    if m:
        s, e = int(m.group(1)), int(m.group(2))
        # datecode form: 4-digit year season + 4-digit MMDD episode -> derive aired
        if len(m.group(1)) == 4 and len(m.group(2)) == 4:
            yy, mm, dd = int(m.group(1)), int(m.group(2)[:2]), int(m.group(2)[2:])
            try:
                aired = date(yy, mm, dd)
            except ValueError:
                pass
        return s, e, aired
    # No code in filename: use the unique per-year number if the caller computed
    # one (performance-name scheme), else fall back to the date's MMDD.
    if ep_override and video in ep_override:
        s, e = ep_override[video]
        return s, e, aired
    if aired:
        return aired.year, int(f"{aired.month:02d}{aired.day:02d}"), aired
    return None, None, aired


CREDIT_PREFIX = "Video source: "
SHOW_CREDIT_PREFIX = "Video sources: "


def channel_of(info):
    return info.get("channel") or info.get("uploader") or ""


# yt-dlp's extractor keys, spelled the way the sites spell themselves.
SITE_NAMES = {"youtube": "YouTube", "youtubetab": "YouTube", "vimeo": "Vimeo",
              "twitch": "Twitch", "twitchvod": "Twitch", "soundcloud": "SoundCloud",
              "bandcamp": "Bandcamp", "archiveorg": "Internet Archive", "nugs": "nugs.net"}


def site_of(info):
    key = info.get("extractor_key") or (info.get("extractor") or "").split(":")[0]
    return SITE_NAMES.get(key.lower(), key) if key else "the web"


def source_credit(info):
    """One plain-text line crediting where the video came from, or ''."""
    ch = channel_of(info)
    if not ch:
        return ""
    page = info.get("channel_url") or info.get("uploader_url") or ""
    vid = info.get("webpage_url") or (f"https://youtu.be/{info['id']}" if info.get("id") else "")
    links = " · ".join(x for x in (page, vid) if x)
    return f"{CREDIT_PREFIX}{ch} on {site_of(info)}" + (f" — {links}" if links else "")


def with_credit(text, credit):
    """Append the credit line to a plot unless one is already there."""
    text = (text or "").rstrip()
    if not credit or CREDIT_PREFIX in text:
        return text
    return f"{text}\n\n{credit}" if text else credit


def show_credit(channels):
    """'Video sources: A (YouTube), B (YouTube)', most-used channel first."""
    if not channels:
        return ""
    ranked = sorted(channels.items(), key=lambda kv: (-kv[1][0], kv[0]))
    return SHOW_CREDIT_PREFIX + ", ".join(f"{name} ({site})" for name, (_, site) in ranked) + "."


def add_credit_in_place(nfo_path, credit, tag_text):
    """Append the credit to an existing NFO's plot (and a tag), touching nothing
    else. Returns True if the file changed."""
    try:
        tree = ET.parse(nfo_path)
    except (ET.ParseError, OSError):
        return False
    root = tree.getroot()
    plot = root.find("plot")
    if plot is None:
        plot = sub(root, "plot", "")
    if credit.startswith(SHOW_CREDIT_PREFIX):
        # The show line is rebuilt each time (a new channel may have appeared).
        lines = [ln for ln in (plot.text or "").split("\n") if not ln.startswith(SHOW_CREDIT_PREFIX)]
        new = "\n".join(lines).rstrip()
        new = f"{new}\n\n{credit}" if new else credit
    else:
        new = with_credit(plot.text, credit)
    changed = new != (plot.text or "")
    plot.text = new
    if tag_text and not any(t.text == tag_text for t in root.findall("tag")):
        sub(root, "tag", tag_text)
        changed = True
    if changed:
        ET.indent(root, space="  ")
        xml = ET.tostring(root, encoding="unicode")
        nfo_path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n' + xml + "\n", encoding="utf-8")
    return changed


def build_episode_nfo(video, info, args, ep_override=None):
    season, episode, aired = season_episode(video, info, ep_override)
    if season is None:
        return None, None  # cannot place it

    root = ET.Element("episodedetails")
    title = info.get("title") or video.stem
    sub(root, "title", title)
    sub(root, "showtitle", args.show)
    sub(root, "season", season)
    sub(root, "episode", episode)
    sub(root, "plot", with_credit(info.get("description") or title, source_credit(info)))
    sub(root, "outline", (info.get("description") or "")[:300] or None)
    if aired:
        iso = aired.isoformat()
        sub(root, "aired", iso)
        sub(root, "premiered", iso)
        sub(root, "year", aired.year)
    studio = args.studio or info.get("channel") or info.get("uploader")
    sub(root, "studio", studio)
    uploader = info.get("uploader") or info.get("channel")
    sub(root, "director", uploader)
    sub(root, "credits", uploader)
    for g in args.genre:
        sub(root, "genre", g)
    sub(root, "tag", "Live")
    if channel_of(info):
        sub(root, "tag", f"Source: {channel_of(info)}")
    if info.get("duration"):
        sub(root, "runtime", max(1, round(info["duration"] / 60)))
    thumb = find_thumb(video)
    if thumb:
        sub(root, "thumb", thumb)
    vid = info.get("id")
    if vid:
        sub(root, "uniqueid", vid, type="youtube", default="true")
        url = info.get("webpage_url") or f"https://youtu.be/{vid}"
        sub(root, "source", url)
    nfo_path = video.parent / (video.name[: -len(video.suffix)] + ".nfo")
    return root, nfo_path


def main():
    ap = argparse.ArgumentParser(description="Generate Plex/Kodi NFO metadata from yt-dlp .info.json files.")
    ap.add_argument("folder", help="The show folder (contains Season subfolders).")
    ap.add_argument("--show", required=True, help='Series name, e.g. "Band A".')
    ap.add_argument("--genre", action="append", default=None, help="Genre (repeatable).")
    ap.add_argument("--studio", default=None, help="Override studio/label.")
    ap.add_argument("--plot", default=None, help="Series description for tvshow.nfo.")
    ap.add_argument("--overwrite", action="store_true", help="Overwrite existing NFO files.")
    ap.add_argument("--add-credits", action="store_true",
                    help="Add the source-channel credit to existing NFOs in place (safe to re-run).")
    args = ap.parse_args()
    if not args.genre:
        args.genre = ["Music", "Concert"]

    root = Path(args.folder).expanduser()
    if not root.is_dir():
        sys.exit(f"Not a folder: {root}")

    videos = sorted(p for p in root.rglob("*") if p.suffix.lower() in VIDEO_EXTS)
    if not videos:
        sys.exit(f"No video files found under {root}")

    written, skipped, unplaced, credited = 0, 0, [], 0
    seasons, all_dates, channel = set(), [], None
    channels = {}   # name -> [episode count, site]

    # Unique per-year episode numbers for the performance-name scheme (files with
    # no SxxExx in the name). Empty for the classic SxxExx layouts.
    ep_map = compute_episode_numbers(videos)

    for v in videos:
        info = load_info(v)
        channel = channel or info.get("channel") or info.get("uploader")
        if channel_of(info):
            c = channels.setdefault(channel_of(info), [0, site_of(info)])
            c[0] += 1
        node, nfo_path = build_episode_nfo(v, info, args, ep_map)
        if node is None:
            unplaced.append(v)
            continue
        season = int(node.find("season").text)
        seasons.add(season)
        aired_el = node.find("aired")
        if aired_el is not None:
            all_dates.append(aired_el.text)
        if write_xml(node, nfo_path, args.overwrite):
            written += 1
        else:
            skipped += 1
            if args.add_credits and source_credit(info) and add_credit_in_place(
                    nfo_path, source_credit(info), f"Source: {channel_of(info)}"):
                credited += 1

    # ---- tvshow.nfo ----
    show_root = ET.Element("tvshow")
    sub(show_root, "title", args.show)
    plot = args.plot or f"Concerts by {args.show}, organised as a series (one season per year/tour, one episode per show)."
    sub(show_root, "plot", f"{plot}\n\n{show_credit(channels)}" if channels else plot)
    for g in args.genre:
        sub(show_root, "genre", g)
    sub(show_root, "studio", args.studio or channel)
    if all_dates:
        first = min(all_dates)
        sub(show_root, "premiered", first)
        sub(show_root, "year", first[:4])
    if write_xml(show_root, root / "tvshow.nfo", args.overwrite):
        written += 1
    else:
        skipped += 1
        if args.add_credits and channels and add_credit_in_place(
                root / "tvshow.nfo", show_credit(channels), None):
            credited += 1

    # ---- season.nfo per season ----
    for s in sorted(seasons):
        # bare year folder (performance-name scheme) or classic "Season <n>"
        folder = root / str(s)
        if not folder.is_dir():
            folder = root / f"Season {s}"
        if not folder.is_dir():
            continue
        se = ET.Element("season")
        sub(se, "seasonnumber", s)
        sub(se, "title", str(s))
        if write_xml(se, folder / "season.nfo", args.overwrite):
            written += 1
        else:
            skipped += 1

    print(f"NFO written: {written}   skipped (already existed): {skipped}")
    if args.add_credits:
        print(f"Source credit added to existing NFOs: {credited}")
    print(f"Seasons: {sorted(seasons)}")
    if unplaced:
        print(f"\n!! Could not place {len(unplaced)} file(s) (no SxxExx in name and no date found):")
        for v in unplaced:
            print(f"   {v.relative_to(root)}")
        print("   Fix their names (or run rename-by-footage-date.py) then re-run.")


if __name__ == "__main__":
    main()
