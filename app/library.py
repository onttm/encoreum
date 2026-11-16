"""
library.py -- read-only browsing of the media library for the UI.

Powers three things: the destination datalist (top-level media folders), the
"does this show already exist / what seasons are present" check, and the tree
viewer. Everything is validated through config.safe_join, so a request can never
list or escape outside ALLOWED_ROOTS. Nothing here ever writes.
"""
import os
from pathlib import Path

from . import config, presets

# NOTE: despite the name, this counts any media file this app can produce --
# video OR audio (presets.MEDIA_EXTS) -- not just video. Keeping the field
# name "videos" rather than renaming it everywhere (API response key, the
# tree UI, show_info's "seasons") would have meant a wider churn for no
# behavior change; the name is now a slight misnomer but every caller already
# treats it as "how many real media files are in here". Before this, an
# audiobook/podcast folder always counted 0 here (audio extensions weren't in
# the set at all), so the browse tree showed every chapter folder as "empty".
def _counts(d: Path) -> tuple[int, int]:
    """(#immediate subdirs, #immediate media files) for a directory."""
    subdirs = videos = 0
    try:
        with os.scandir(d) as it:
            for e in it:
                try:
                    if e.is_dir():
                        subdirs += 1
                    elif e.is_file() and os.path.splitext(e.name)[1].lower() in presets.MEDIA_EXTS:
                        videos += 1
                except OSError:
                    continue
    except OSError:
        pass
    return subdirs, videos


# yt-dlp leftovers from interrupted / failed downloads (e.g. a run that 403'd
# partway leaves .part / .ytdl / fragment files). These are never real media.
PARTIAL_GLOBS = ("*.part", "*.ytdl", "*.part-Frag*", "*.temp")


def sweep_partials(rel: str = "") -> dict:
    """
    Delete orphaned partial-download files under LIBRARY_ROOT/rel and report how
    many were removed and bytes freed. Only ever touches yt-dlp partial artifacts
    (never videos, NFO, thumbnails, or posters). Validated against ALLOWED_ROOTS.
    """
    target = config.safe_join(config.LIBRARY_ROOT, config.clean_dest(rel))
    removed, freed = 0, 0
    if not target.is_dir():
        return {"removed": 0, "bytes": 0, "path": config.clean_dest(rel)}
    for pattern in PARTIAL_GLOBS:
        for p in target.rglob(pattern):
            try:
                if p.is_file():
                    freed += p.stat().st_size
                    p.unlink()
                    removed += 1
            except OSError:
                continue
    return {"removed": removed, "bytes": freed, "path": config.clean_dest(rel)}


def browse(rel: str = "") -> dict:
    """
    List the immediate subfolders of ``LIBRARY_ROOT/rel`` with a light summary
    (subfolders + video count each). Used for the datalist, existence check and
    the lazy tree viewer. Raises ValueError if ``rel`` escapes ALLOWED_ROOTS.
    """
    target = config.safe_join(config.LIBRARY_ROOT, config.clean_dest(rel))
    rel_norm = config.clean_dest(rel)
    if not target.is_dir():
        return {"path": rel_norm, "exists": False, "entries": [], "loose_videos": 0}

    entries = []
    loose_videos = 0
    try:
        with os.scandir(target) as it:
            for e in it:
                try:
                    if e.is_dir():
                        subdirs, videos = _counts(Path(e.path))
                        entries.append({"name": e.name, "subdirs": subdirs, "videos": videos})
                    elif e.is_file() and os.path.splitext(e.name)[1].lower() in presets.MEDIA_EXTS:
                        loose_videos += 1
                except OSError:
                    continue
    except OSError:
        pass

    entries.sort(key=lambda x: x["name"].lower())
    return {"path": rel_norm, "exists": True, "entries": entries, "loose_videos": loose_videos}


def show_info(destination: str, show: str) -> dict:
    """
    Existence + seasons for a specific show under a destination:
        {exists, path, seasons: [{name, videos}], total_videos}
    'seasons' are the show's immediate subfolders; 'name' is shown verbatim.
    """
    show = (show or "").strip().strip("/")
    if not show:
        return {"exists": False, "path": "", "seasons": [], "total_videos": 0}
    dest = config.clean_dest(destination)
    rel = f"{dest}/{show}" if dest else show
    listing = browse(rel)
    seasons = [{"name": e["name"], "videos": e["videos"]} for e in listing["entries"]]
    total = sum(s["videos"] for s in seasons) + listing["loose_videos"]
    return {
        "exists": listing["exists"],
        "path": listing["path"],
        "seasons": seasons,
        "loose_videos": listing["loose_videos"],
        "total_videos": total,
    }
