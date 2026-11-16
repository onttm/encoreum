#!/usr/bin/env python3
"""
renumber-episodes.py
=========================================================================
Collision-free episode numbering for concert libraries.

datecode (S<year>E<mmdd>) breaks when several videos share a date (e.g. a
per-song playlist: every song from one night gets the same S2026E0621, which
Plex then treats as the SAME episode). This renumbers each show to:

        Season <concert-year>/<Show> - S<year>E<nn> - <title>.<ext>

with episodes numbered **sequentially within each concert year, ordered by the
concert (footage) date**. Numbers are unique, so Plex never merges episodes, and
the true date still lands in the NFO (via generate-nfo.py) for date sorting.

It reuses the exact date parser from rename-by-footage-date.py, so dating is
identical to the rest of the pipeline. It only reads .info.json and renames
videos + their sidecars; it never touches file contents.

USAGE
    python3 renumber-episodes.py "/path/to/Concerts/Band A" --show "Band A" [--apply]

Note: numbering is derived from the whole current file set. Adding more videos
later can shift numbers, so download a tour/playlist in one go where possible.
=========================================================================
"""
import argparse
import importlib.util
import json
import sys
from collections import defaultdict
from pathlib import Path

# Reuse find_date / date_from_meta / clean / move_with_sidecars / *_EXTS.
_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "cdl_rename_footage", str(_HERE / "rename-by-footage-date.py"))
_R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_R)


def _load_info(video: Path) -> dict:
    p = video.parent / (video.name[: -len(video.suffix)] + ".info.json")
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def main():
    ap = argparse.ArgumentParser(description="Renumber concerts to collision-free S<year>E<nn> by concert date.")
    ap.add_argument("folder", help="The show folder (may contain Season subfolders).")
    ap.add_argument("--show", required=True, help='Series name, e.g. "Band A".')
    ap.add_argument("--apply", action="store_true", help="Actually rename (default is a dry-run preview).")
    args = ap.parse_args()

    root = Path(args.folder).expanduser()
    if not root.is_dir():
        sys.exit(f"Not a folder: {root}")
    videos = [p for p in root.rglob("*") if p.suffix.lower() in _R.MEDIA_EXTS]
    if not videos:
        sys.exit(f"No video files found under {root}")

    print(f"{'APPLYING' if args.apply else 'PREVIEW (no changes)'} - {len(videos)} videos\n")

    dated, undated = [], []
    for v in sorted(videos):
        info = _load_info(v)
        title = info.get("title") or v.stem
        date = (_R.find_date(title) or _R.find_date(info.get("description", ""))
                or _R.date_from_meta(info))
        if date:
            dated.append((date, v, title))
        else:
            undated.append(v)

    by_year = defaultdict(list)
    for date, v, title in dated:
        by_year[date[0]].append((date, v, title))

    plan = []
    for year in sorted(by_year):
        entries = sorted(by_year[year], key=lambda e: (e[0], e[1].name))
        pad = max(2, len(str(len(entries))))
        for i, (date, v, title) in enumerate(entries, start=1):
            new_name = f"{args.show} - S{year:04d}E{i:0{pad}d} - {_R.clean(title)}{v.suffix}"
            plan.append((v, root / f"Season {year}" / new_name))

    for v, new_path in plan:
        print(f"{v.relative_to(root)}\n   -> {new_path.relative_to(root)}")

    if args.apply:
        moved = 0
        for v, new_path in plan:
            if v.resolve() == new_path.resolve():
                continue
            moved += _R.move_with_sidecars(v, new_path)
        # tidy up any now-empty Season folders left behind by the moves
        for d in sorted(root.glob("Season *"), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        print(f"\nDone. Renumbered {len(plan)} videos ({moved} files moved).")
    else:
        print(f"\nDry run only. Re-run with --apply to make these {len(plan)} changes.")

    if undated:
        print(f"\n!! Could NOT date {len(undated)} file(s) - left in place:")
        for v in undated:
            print(f"   {v.relative_to(root)}")


if __name__ == "__main__":
    main()
