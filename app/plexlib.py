"""
plexlib.py -- read Plex's libraries: where they live, and what has been watched.

THREE NAMES FOR ONE DIRECTORY
    The same folder is called different things by everyone who touches it:

        host          /mnt/nas/media/concerts
        Plex          /data/media1/concerts      (Plex's own bind mount)
        this app      /library/concerts          (our bind mount)

    So "just read the path from Plex" does not work — Plex reports ITS view,
    which does not exist in this container. map_plex_path() resolves this by
    walking the reported path from the right and testing which suffix actually
    exists under our own library root. No hardcoded prefixes, so it keeps
    working if either side is remounted.

WHAT THIS IS FOR
    * Auto-configuring a download destination from a real Plex library, instead
      of asking someone to type a path that has to match one they cannot see.
    * Read-state (viewedLeafCount / lastViewedAt), which is what makes a
      retention policy possible: "recorded six months ago, never played".
    * Warning when a target library's agent cannot read the metadata we write.
"""
from __future__ import annotations

import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from . import config, secrets

PLEX_URL = os.environ.get("PLEX_URL", "http://172.17.0.1:32400").rstrip("/")

# Plex library types that can hold a video concert.
VIDEO_TYPES = {"show", "movie"}

# Agents that can read the Kodi/XBMC NFO files this app writes. Anything else
# will ignore our metadata entirely and fall back to filename guessing.
NFO_AGENTS = {"tv.plex.agents.nfo.series", "xbmcnfotv", "com.plexapp.agents.xbmcnfotv"}


def _token() -> str:
    # File-based by preference. A Plex token in an env var is inherited by
    # yt-dlp, streamlink and ffmpeg, which this app spawns constantly.
    return secrets.read("PLEX_TOKEN")


def _get(path: str, **params) -> ET.Element | None:
    tok = _token()
    if not tok:
        return None
    params["X-Plex-Token"] = tok
    url = f"{PLEX_URL}{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return ET.fromstring(r.read())
    except Exception:  # noqa: BLE001
        return None


def map_plex_path(plex_path: str) -> str | None:
    """Translate a Plex-reported path into a path inside THIS container.

    Walks the path from the right: /data/media1/concerts -> try
    <root>/data/media1/concerts, then <root>/media1/concerts, then
    <root>/concerts. The first that exists on disk wins.

    Returns None if nothing matches, which is a real answer — it means the two
    containers do not share that directory, and silently guessing would be worse.
    """
    if not plex_path:
        return None
    parts = [p for p in Path(plex_path).parts if p not in ("/", "")]
    root = Path(config.LIBRARY_ROOT)
    for i in range(len(parts)):
        cand = root.joinpath(*parts[i:])
        if cand.is_dir():
            return str(cand)
    return None


def sections() -> list[dict]:
    """Every Plex library, with its on-disk path translated to ours."""
    root = _get("/library/sections")
    if root is None:
        return []
    out = []
    for d in root.findall("Directory"):
        plex_paths = [l.get("path") for l in d.findall("Location") if l.get("path")]
        mapped = [(p, map_plex_path(p)) for p in plex_paths]
        agent = d.get("agent") or ""
        out.append({
            "key": d.get("key"),
            "title": d.get("title"),
            "type": d.get("type"),
            "agent": agent,
            "plex_paths": plex_paths,
            "local_paths": [m for _, m in mapped if m],
            "unreachable_paths": [p for p, m in mapped if not m],
            "reachable": any(m for _, m in mapped),
            "reads_nfo": agent in NFO_AGENTS,
            "usable_for_concerts": d.get("type") in VIDEO_TYPES,
        })
    return out


def target_libraries() -> list[dict]:
    """Libraries this app could actually write concerts into, best first.

    Ranked by whether we can reach the folder at all, then whether the agent
    can read the NFO metadata we write — an agent of `none` means every file
    lands with no metadata whatsoever, which looks like the app is broken.
    """
    cands = [s for s in sections() if s["usable_for_concerts"]]
    for c in cands:
        score = 0
        notes = []
        if c["reachable"]:
            score += 10
        else:
            notes.append("this container cannot see that folder — check the "
                         "library volume mount")
        if c["reads_nfo"]:
            score += 5
        elif c["agent"].endswith(".none"):
            notes.append("agent is 'none': files will carry NO metadata at all")
        else:
            notes.append(f"agent {c['agent'].split('.')[-1]!r} will not read our "
                         "NFO files; set the library agent to 'Plex NFO Series'")
        if c["type"] == "show":
            score += 3          # seasons-by-year is the shape we already write
        c["score"], c["notes"] = score, notes
    return sorted(cands, key=lambda c: -c["score"])


def watch_state(section_key: str, limit: int = 400) -> list[dict]:
    """Per-item read-state — the basis for any retention policy.

    Plex reports viewedLeafCount/leafCount for a show and lastViewedAt when it
    has been played. "Recorded, never watched, six months old" is exactly the
    thing worth surfacing before a disk fills up.
    """
    root = _get(f"/library/sections/{section_key}/all")
    if root is None:
        return []
    out = []
    for n in list(root)[:limit]:
        if n.tag not in ("Directory", "Video"):
            continue
        leaf = int(n.get("leafCount") or 0)
        seen = int(n.get("viewedLeafCount") or 0)
        lva = n.get("lastViewedAt")
        out.append({
            "title": n.get("title"),
            "rating_key": n.get("ratingKey"),
            "added_at": int(n.get("addedAt") or 0) or None,
            "last_viewed_at": int(lva) if lva else None,
            "episodes": leaf,
            "watched_episodes": seen,
            "fully_watched": bool(leaf and seen >= leaf),
            "never_watched": seen == 0,
        })
    return out


def unwatched_older_than(section_key: str, days: int = 180) -> list[dict]:
    """Candidates for deletion: added a while ago, never played."""
    import time
    cutoff = time.time() - days * 86400
    return [i for i in watch_state(section_key)
            if i["never_watched"] and i["added_at"] and i["added_at"] < cutoff]
