#!/usr/bin/env python3
"""
rename-by-footage-date.py
=========================================================================
Rename downloaded concert videos to Plex "date-code" episodes:

        <Show> - S<YYYY>E<MMDD> - <title>.mkv      (Season <YYYY> folder)
        e.g.   Band A - S2023E0427 - Amsterdam Night 1.mkv

It figures out the *footage* (concert) date by looking, in order, at:
    1. a date written in the video TITLE
    2. a date written in the video DESCRIPTION
    3. the release/upload date (fallback)

Both plain dates (2023-04-27, 27.04.2023) and written dates
("April 27, 2023", "27 April 2023") are understood.

It relies on the .info.json files that the download script saved next to
each video (the script has --write-info-json turned on).

---------------------------------------------------------------------------
USAGE
    # 1) PREVIEW ONLY (nothing is changed) - always do this first:
    python3 rename-by-footage-date.py "/path/to/Concerts/Band A" --show "Band A"

    # 2) When the preview looks right, actually rename/move the files:
    python3 rename-by-footage-date.py "/path/to/Concerts/Band A" --show "Band A" --apply

The path you pass is the SHOW folder (it may already contain Season folders).
Files it cannot date are listed at the end so you can handle those few by hand.
=========================================================================
"""

import argparse
import json
import re
import sys
from pathlib import Path

VIDEO_EXTS = {".mkv", ".mp4", ".webm", ".m4v", ".avi", ".mov"}
# The "Audio only" checkbox (any naming mode, not just the dedicated audio
# presets) extracts audio-only into these same rename/renumber pipelines with
# no forced re-encode, so the real container varies by source.
AUDIO_EXTS = {".opus", ".m4a", ".mp3", ".flac", ".ogg", ".oga", ".wav", ".aac"}
# What counts as "a media file to rename" -- video OR audio. Kept separate
# from VIDEO_EXTS because generate-nfo.py/generate-plex-art.py are genuinely
# video-only (Kodi/Plex TV metadata) and must keep using VIDEO_EXTS alone.
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS
# Sidecar files that should travel with the video when we rename it.
SIDECAR_EXTS = [".info.json", ".jpg", ".png", ".webp", ".nfo", ".en.srt", ".srt"]

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))

# Date patterns, tried in this order. Each returns (year, month, day).
PATTERNS = [
    # 2023-04-27 / 2023.04.27 / 2023/04/27
    (re.compile(r"\b(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})\b"),
     lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    # April 27, 2023  /  Apr 27 2023
    (re.compile(r"\b(" + MONTH_RE + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})\b", re.I),
     lambda m: (int(m[3]), MONTHS[m[1].lower()], int(m[2]))),
    # 27 April 2023  /  27th Apr 2023
    (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + MONTH_RE + r")\.?,?\s+(20\d{2})\b", re.I),
     lambda m: (int(m[3]), MONTHS[m[2].lower()], int(m[1]))),
    # 27-04-2023 / 27.04.2023  (day-first; only trusted when first number > 12)
    (re.compile(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](20\d{2})\b"),
     lambda m: (int(m[3]), int(m[2]), int(m[1])) if int(m[1]) > 12 else (int(m[3]), int(m[1]), int(m[2]))),
]

ILLEGAL = re.compile(r'[\\/:*?"<>|]')


def find_date(text):
    """Return (year, month, day) from free text, or None."""
    if not text:
        return None
    for regex, build in PATTERNS:
        for m in regex.finditer(text):
            try:
                y, mo, d = build(m)
                if 1 <= mo <= 12 and 1 <= d <= 31 and 1900 <= y <= 2100:
                    return (y, mo, d)
            except (KeyError, ValueError):
                continue
    return None


def date_from_meta(info):
    """Fallback: release_date or upload_date (YYYYMMDD string)."""
    for key in ("release_date", "upload_date"):
        v = info.get(key)
        if v and len(str(v)) == 8 and str(v).isdigit():
            v = str(v)
            return (int(v[:4]), int(v[4:6]), int(v[6:8]))
    return None


def clean(title):
    title = ILLEGAL.sub(" ", title or "").strip()
    title = re.sub(r"\s+", " ", title)
    return title[:150].rstrip(" .")


def move_with_sidecars(video, new_video):
    """Rename the video and any matching sidecar files, return count moved."""
    new_video.parent.mkdir(parents=True, exist_ok=True)
    stem = video.name[: -len(video.suffix)]
    moved = 0
    for f in video.parent.iterdir():
        if not f.name.startswith(stem):
            continue
        suffix = f.name[len(stem):]  # e.g. ".info.json", ".jpg"
        if f == video:
            target = new_video
        elif suffix in SIDECAR_EXTS or suffix.startswith("."):
            target = new_video.parent / (new_video.name[: -len(new_video.suffix)] + suffix)
        else:
            continue
        f.rename(target)
        moved += 1
    return moved


def main():
    ap = argparse.ArgumentParser(description="Rename concert videos to S<YYYY>E<MMDD> using footage date.")
    ap.add_argument("folder", help="The show folder (may contain Season subfolders).")
    ap.add_argument("--show", required=True, help='Series name, e.g. "Band A".')
    ap.add_argument("--apply", action="store_true", help="Actually rename (default is a dry-run preview).")
    args = ap.parse_args()

    root = Path(args.folder).expanduser()
    if not root.is_dir():
        sys.exit(f"Not a folder: {root}")

    videos = [p for p in root.rglob("*") if p.suffix.lower() in MEDIA_EXTS]
    if not videos:
        sys.exit(f"No video/audio files found under {root}")

    print(f"{'PREVIEW (no changes)' if not args.apply else 'APPLYING CHANGES'} - {len(videos)} videos\n")

    used_codes = {}   # (year, mmdd) -> count, to spot same-day collisions
    unmatched = []
    plan = []

    for v in sorted(videos):
        info_path = v.with_suffix(v.suffix + ".info.json")
        # yt-dlp names it "<basename>.info.json" (basename without video ext)
        alt_info = v.parent / (v.name[: -len(v.suffix)] + ".info.json")
        info = {}
        for p in (info_path, alt_info):
            if p.exists():
                try:
                    info = json.loads(p.read_text(encoding="utf-8"))
                    break
                except Exception:
                    pass

        title = info.get("title") or v.stem
        source = None
        date = find_date(title)
        if date:
            source = "title"
        if not date:
            date = find_date(info.get("description", ""))
            if date:
                source = "description"
        if not date:
            date = date_from_meta(info)
            if date:
                source = "upload/release date"

        if not date:
            unmatched.append(v)
            continue

        y, mo, d = date
        mmdd = f"{mo:02d}{d:02d}"
        key = (y, mmdd)
        used_codes[key] = used_codes.get(key, 0) + 1
        dup = f"  <-- DUPLICATE of same date ({used_codes[key]})" if used_codes[key] > 1 else ""

        new_name = f"{args.show} - S{y:04d}E{mmdd} - {clean(title)}{v.suffix}"
        new_path = root / f"Season {y}" / new_name
        plan.append((v, new_path, source, dup))

    for v, new_path, source, dup in plan:
        rel_old = v.relative_to(root)
        rel_new = new_path.relative_to(root)
        print(f"[{source:>18}] {rel_old}\n   -> {rel_new}{dup}\n")

    if args.apply:
        moved = 0
        for v, new_path, _, _ in plan:
            if v.resolve() == new_path.resolve():
                continue
            moved += move_with_sidecars(v, new_path)
        print(f"Done. Moved/renamed {moved} files ({len(plan)} videos).")
    else:
        print(f"Dry run only. Re-run with --apply to make these {len(plan)} changes.")

    if unmatched:
        print(f"\n!! Could NOT find a date for {len(unmatched)} file(s) - handle these by hand:")
        for v in unmatched:
            print(f"   {v.relative_to(root)}")


if __name__ == "__main__":
    main()
