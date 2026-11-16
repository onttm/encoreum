#!/usr/bin/env python3
"""
generate-plex-art.py
=========================================================================
Emit **Plex-native** local artwork so a concert library looks polished with
Plex's built-in "Local Media Assets" agent -- *no plugin required* for images.

Plex reads these filenames natively:
    <show>/poster.jpg            -> the series poster
    <show>/fanart.jpg            -> the series background
    <show>/Season<NN>.jpg        -> a season poster (show-root convention)
    <show>/Season <NN>/poster.jpg-> a season poster (in-folder fallback)
    <show>/Season <NN>/<video>.jpg -> the per-episode thumbnail (yt-dlp already
                                       writes this; we don't touch it here)

We have no real "artist poster", so we reuse the earliest episode's thumbnail
as the show/season art. It gives Plex something sensible; the user can drop a
nicer poster.jpg in later and it will win (we never overwrite unless --overwrite).

It only ever *copies* existing .jpg thumbnails into the poster filenames Plex
looks for. It never touches your videos, NFO, or the thumbnails themselves.

USAGE
    python3 generate-plex-art.py "/path/to/Concerts/Band A" --show "Band A"
=========================================================================
"""
import argparse
import re
import shutil
import sys
from pathlib import Path

VIDEO_EXTS = {".mkv", ".mp4", ".webm", ".m4v", ".avi", ".mov"}
IMG_EXTS = [".jpg", ".jpeg", ".png"]
SEASON_RE = re.compile(r"Season\s+(\d+)", re.I)


def thumb_for(video: Path):
    """The sidecar image yt-dlp wrote next to a video, if any."""
    base = video.name[: -len(video.suffix)]
    for ext in IMG_EXTS:
        p = video.parent / (base + ext)
        if p.exists():
            return p
    return None


def copy_art(src: Path, dst: Path, overwrite: bool) -> bool:
    if dst.exists() and not overwrite:
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return True


def main():
    ap = argparse.ArgumentParser(description="Generate Plex-native local artwork from episode thumbnails.")
    ap.add_argument("folder", help="The show folder (contains Season subfolders).")
    ap.add_argument("--show", required=True, help='Series name (for logging only).')
    ap.add_argument("--overwrite", action="store_true", help="Replace artwork that already exists.")
    args = ap.parse_args()

    root = Path(args.folder).expanduser()
    if not root.is_dir():
        sys.exit(f"Not a folder: {root}")

    videos = sorted(p for p in root.rglob("*") if p.suffix.lower() in VIDEO_EXTS)
    if not videos:
        sys.exit(f"No video files found under {root}")

    # Group the earliest thumbnail per season (videos sort chronologically thanks
    # to the SxxExx / date-code naming), and remember the overall earliest.
    season_thumb: dict[int, Path] = {}
    show_thumb: Path | None = None
    for v in videos:
        m = SEASON_RE.search(str(v.parent.name))
        if not m:
            continue
        season = int(m.group(1))
        thumb = thumb_for(v)
        if thumb is None:
            continue
        season_thumb.setdefault(season, thumb)
        if show_thumb is None:
            show_thumb = thumb

    if show_thumb is None:
        print("No episode thumbnails (.jpg) found yet - nothing to do.")
        return

    made = 0
    # --- series poster + background
    if copy_art(show_thumb, root / "poster.jpg", args.overwrite):
        made += 1
    if copy_art(show_thumb, root / "fanart.jpg", args.overwrite):
        made += 1

    # --- season posters (show-root convention + in-folder fallback)
    for season, thumb in sorted(season_thumb.items()):
        if copy_art(thumb, root / f"Season{season}.jpg", args.overwrite):
            made += 1
        folder = root / f"Season {season}"
        if folder.is_dir() and copy_art(thumb, folder / "poster.jpg", args.overwrite):
            made += 1

    print(f"Plex artwork: wrote {made} image(s) for '{args.show}' "
          f"(seasons: {sorted(season_thumb)}).")
    if made == 0:
        print("(Everything already existed - re-run with --overwrite to refresh.)")


if __name__ == "__main__":
    main()
