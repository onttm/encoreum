"""
sponsorblock.py -- find SponsorBlock's crowd-sourced sponsor segments, and add
them to files ALREADY in the library as chapter MARKS. Never cuts anything.

EXTERNAL SERVICE (not Claude/Anthropic, no account, no key, free):
    sponsor.ajay.app. Its data is licensed CC BY-NC-SA 4.0 -- non-commercial,
    attribution required -- so every page that shows it credits SponsorBlock.
    If the service is down or our budget is spent, lookups report that plainly
    and nothing else changes; Encoreum works the same without it.

PRIVACY
    Uses the hash-prefix endpoint: only the first 4 hex chars of
    sha256(videoID) leave this machine. The server returns every video sharing
    that prefix and the match happens HERE, so SponsorBlock never learns which
    video is in this library.

WHY MARKS, NOT CUTS (see CLAUDE.md "Chapters, sponsor marks...")
    Only sponsor + selfpromo. intro/outro/filler/music_offtopic get applied to
    walk-ons, banter and encore breaks on concerts. A mark is reversible: the
    file's original chapter list is backed up before the first change and
    `undo()` puts it back exactly.

SAME RESULT AS THE DOWNLOAD CHECKBOX
    merge_chapters() reproduces what `yt-dlp --sponsorblock-mark` was measured
    to produce on real videos (2026-11-16): an ad inside a chapter splits it
    (X -> [SponsorBlock]: Sponsor -> X); one ending on a boundary just
    shortens the chapter; a chapterless file becomes title -> Sponsor -> title.
    Chapter titles use yt-dlp's exact names, so `already_marked` recognises
    files from either path.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config, plexlib, presets, ratelimit

API = "https://sponsor.ajay.app/api/skipSegments/"
USER_AGENT = "Encoreum (self-hosted; sponsor chapter marks)"
CATEGORIES = ("sponsor", "selfpromo")
# yt-dlp's own names (SponsorBlockPP.CATEGORIES), so files marked at download
# time and files marked here look identical.
LABELS = {"sponsor": "Sponsor", "selfpromo": "Unpaid/Self Promotion"}
MARK_PREFIX = "[SponsorBlock]: "
ATTRIBUTION = ("Sponsor segments from SponsorBlock (sponsor.ajay.app), "
               "CC BY-NC-SA 4.0")

# ffmpeg writes chapters reliably into these containers with a stream copy.
# Others (.opus/.ogg/.webm/.mp3) are reported but not modified.
MARKABLE_EXTS = {".mkv", ".mp4", ".m4v", ".m4a", ".mov"}
# yt-dlp drops chapters shorter than this when it splits; so do we.
TINY = 1.0
CACHE_TTL = 6 * 3600
BACKUP_DIR = config.CONFIG_DIR / "chapter-backups"
SCAN_FILE = config.CONFIG_DIR / "sponsorscan.json"

_cache: dict[str, tuple[float, list]] = {}
_cache_lock = threading.Lock()


# --- lookup -------------------------------------------------------------------

def _fetch_prefix(prefix: str) -> list | None:
    """All videos sharing a 4-char hash prefix. [] = none; None = failed."""
    ok, _reason = ratelimit.acquire("sponsorblock")
    if not ok:
        return None
    q = urllib.parse.urlencode({"categories": json.dumps(list(CATEGORIES)),
                                "actionType": "skip"})
    req = urllib.request.Request(API + prefix + "?" + q,
                                 headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return [] if e.code == 404 else None      # 404 = nothing for this prefix
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def lookup(video_ids: list[str]) -> dict:
    """{"segments": {id: [{"category","start","end"}...]}, "failed": [ids], "status": str}.

    An id absent from `segments` and `failed` has no segments. status is "ok",
    "partial" (some lookups failed) or "unavailable" (all failed: service down
    or our daily budget is spent -- both mean 'try later', not an error).
    """
    ids = [v for v in dict.fromkeys(video_ids) if v]
    out: dict[str, list] = {}
    failed: list[str] = []
    now = time.time()
    by_prefix: dict[str, list[str]] = {}
    for vid in ids:
        with _cache_lock:
            hit = _cache.get(vid)
        if hit and now - hit[0] < CACHE_TTL:
            if hit[1]:
                out[vid] = hit[1]
            continue
        by_prefix.setdefault(hashlib.sha256(vid.encode()).hexdigest()[:4], []).append(vid)
    for prefix, vids in by_prefix.items():
        data = _fetch_prefix(prefix)
        if data is None:
            failed += vids
            continue
        found = {d.get("videoID"): d.get("segments") or [] for d in data}
        for vid in vids:
            segs = sorted(
                ({"category": s["category"], "start": float(s["segment"][0]),
                  "end": float(s["segment"][1])}
                 for s in found.get(vid, [])
                 if s.get("category") in CATEGORIES and s.get("actionType", "skip") == "skip"
                 and len(s.get("segment") or []) == 2),
                key=lambda s: s["start"])
            with _cache_lock:
                _cache[vid] = (now, segs)
            if segs:
                out[vid] = segs
    status = "ok" if not failed else ("unavailable" if len(failed) == len(ids) else "partial")
    return {"segments": out, "failed": failed, "status": status,
            "attribution": ATTRIBUTION}


# --- chapters -----------------------------------------------------------------

def _probe(path: Path) -> tuple[list[dict], float]:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_chapters", "-show_format",
                        "-of", "json", str(path)], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {r.stderr.strip()[:200]}")
    j = json.loads(r.stdout or "{}")
    chs = [{"start": float(c["start_time"]), "end": float(c["end_time"]),
            "title": (c.get("tags") or {}).get("title", "")} for c in j.get("chapters", [])]
    return chs, float((j.get("format") or {}).get("duration") or 0)


def merge_chapters(chapters: list[dict], segments: list[dict], duration: float,
                   fallback_title: str) -> list[dict]:
    """Insert sponsor chapters, splitting whatever they overlap (see module doc)."""
    base = chapters or [{"start": 0.0, "end": duration, "title": fallback_title}]
    # Clamp, drop junk, and stop sponsor segments overlapping each other.
    segs, last = [], 0.0
    for s in sorted(segments, key=lambda s: s["start"]):
        a, b = max(s["start"], last, 0.0), min(s["end"], duration or s["end"])
        if b - a >= TINY:
            segs.append({"start": a, "end": b,
                         "title": MARK_PREFIX + LABELS.get(s["category"], s["category"])})
            last = b
    out = []
    for ch in base:
        pieces = [(ch["start"], ch["end"])]
        for s in segs:
            nxt = []
            for a, b in pieces:
                if s["end"] <= a or s["start"] >= b:
                    nxt.append((a, b))
                    continue
                if s["start"] > a:
                    nxt.append((a, s["start"]))
                if s["end"] < b:
                    nxt.append((s["end"], b))
            pieces = nxt
        out += [{"start": a, "end": b, "title": ch["title"]} for a, b in pieces if b - a >= TINY]
    return sorted(out + segs, key=lambda c: c["start"])


def _ffmeta(chapters: list[dict]) -> str:
    def esc(s: str) -> str:
        for c in ("\\", "=", ";", "#", "\n"):
            s = s.replace(c, "\\" + c)
        return s
    lines = [";FFMETADATA1"]
    for c in chapters:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000",
                  f"START={int(round(c['start'] * 1000))}",
                  f"END={int(round(c['end'] * 1000))}",
                  f"title={esc(c['title'])}"]
    return "\n".join(lines) + "\n"


def _rewrite_chapters(path: Path, chapters: list[dict]) -> None:
    """Replace a file's chapter list with a stream copy, atomically.

    The new file is built in STAGING_DIR (same filesystem as the library, never
    scanned by Plex), checked, and only then os.replace()d over the original --
    so nothing half-written is ever visible in the library.
    """
    _, duration = _probe(path)
    config.STAGING_DIR.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(prefix="sponsormarks-", dir=config.STAGING_DIR))
    try:
        meta = tmpdir / "chapters.txt"
        meta.write_text(_ffmeta(chapters), encoding="utf-8")
        out = tmpdir / ("out" + path.suffix.lower())
        cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path)]
        if chapters:
            cmd += ["-f", "ffmetadata", "-i", str(meta), "-map_chapters", "1"]
        else:
            cmd += ["-map_chapters", "-1"]
        cmd += ["-map", "0", "-map_metadata", "0", "-c", "copy", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {r.stderr.strip()[-300:]}")
        got, new_dur = _probe(out)
        if abs(new_dur - duration) > 1.0:
            raise RuntimeError(f"length changed ({duration:.0f}s -> {new_dur:.0f}s); left original alone")
        if len(got) != len(chapters):
            raise RuntimeError(f"wrote {len(got)} chapters, expected {len(chapters)}; left original alone")
        shutil.copymode(path, out)
        os.replace(out, path)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# --- library files ------------------------------------------------------------

def _resolve(rel: str) -> Path:
    p = config.safe_join(config.LIBRARY_ROOT, rel)
    if config.STAGING_DIR in p.parents or not p.is_file():
        raise ValueError(f"not a library file: {rel}")
    return p


def _info(media: Path) -> dict:
    side = media.with_name(media.stem + ".info.json")
    if not side.is_file():
        return {}
    try:
        j = json.loads(side.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {"id": j.get("id") or "", "title": j.get("title") or "",
            "extractor": (j.get("extractor_key") or j.get("extractor") or "").lower()}


def _backup_path(rel: str) -> Path:
    return BACKUP_DIR / (hashlib.sha1(rel.encode()).hexdigest() + ".json")


def _plex_refresh(media: Path) -> str:
    """Ask Plex to rescan the episode's folder so it re-reads the chapters.

    Best effort: returns a sentence for the UI either way. Plex would notice the
    changed file on its next scheduled scan regardless.
    """
    folder = str(media.parent)
    if not plexlib._token():
        return "No Plex token configured; Plex will pick up the new chapters on its next library scan."
    try:
        for s in plexlib.sections():
            for plex_path, local in zip(s["plex_paths"], [plexlib.map_plex_path(p) for p in s["plex_paths"]]):
                if local and (folder == local or folder.startswith(local.rstrip("/") + "/")):
                    plex_folder = plex_path.rstrip("/") + folder[len(local.rstrip("/")):]
                    plexlib._get(f"/library/sections/{s['key']}/refresh", path=plex_folder)
                    return f"Asked Plex to rescan this folder ({s['title']})."
    except Exception:  # noqa: BLE001
        pass
    return "Plex wasn't reachable; it will pick up the new chapters on its next library scan."


def apply_marks(rel: str) -> dict:
    """Add sponsor chapter marks to one library file. Idempotent."""
    path = _resolve(rel)
    if path.suffix.lower() not in MARKABLE_EXTS:
        return {"ok": False, "message": f"Can't add chapters to {path.suffix} files safely."}
    info = _info(path)
    if not info.get("id"):
        return {"ok": False, "message": "No .info.json with a video ID next to this file."}
    chapters, duration = _probe(path)
    if any(c["title"].startswith(MARK_PREFIX) for c in chapters):
        return {"ok": True, "message": "Already has sponsor marks; nothing changed."}
    res = lookup([info["id"]])
    if info["id"] in res["failed"]:
        return {"ok": False, "message": "SponsorBlock lookup failed (service down or budget spent). Try later."}
    segs = res["segments"].get(info["id"], [])
    if not segs:
        return {"ok": True, "message": "No sponsor segments for this video; nothing changed."}
    new = merge_chapters(chapters, segs, duration, info.get("title") or path.stem)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    bk = _backup_path(rel)
    if not bk.exists():          # never overwrite the true original
        bk.write_text(json.dumps({"path": rel, "chapters": chapters, "saved": int(time.time())},
                                 indent=1), encoding="utf-8")
    _rewrite_chapters(path, new)
    _set_scan_row(rel, marked=True)
    return {"ok": True, "message": f"Added {len(segs)} sponsor mark(s). " + _plex_refresh(path),
            "chapters": new}


def undo_marks(rel: str) -> dict:
    """Put back the chapter list saved before marks were first added."""
    path = _resolve(rel)
    bk = _backup_path(rel)
    if not bk.is_file():
        return {"ok": False, "message": "No saved original chapters for this file."}
    orig = json.loads(bk.read_text(encoding="utf-8"))["chapters"]
    _rewrite_chapters(path, orig)
    bk.unlink()
    _set_scan_row(rel, marked=False)
    return {"ok": True, "message": "Original chapters restored. " + _plex_refresh(path)}


# --- library scan (background) ------------------------------------------------

_scan_lock = threading.Lock()
_scan: dict = {"state": "idle"}


def _load_scan() -> dict:
    try:
        return json.loads(SCAN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "idle"}


def _save_scan(s: dict) -> None:
    try:
        tmp = SCAN_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(s), encoding="utf-8")
        os.replace(tmp, SCAN_FILE)
    except OSError:
        pass


def _set_scan_row(rel: str, marked: bool) -> None:
    with _scan_lock:
        s = _scan if _scan.get("state") != "idle" else _load_scan()
        for f in s.get("findings", []):
            if f["path"] == rel:
                f["marked"] = marked
                f["can_undo"] = _backup_path(rel).is_file()
        _scan.clear(); _scan.update(s)
        _save_scan(s)


def scan_status() -> dict:
    with _scan_lock:
        if _scan.get("state") == "idle":
            _scan.update(_load_scan())
            if _scan.get("state") == "running":     # interrupted by a restart
                _scan["state"] = "interrupted"
        return json.loads(json.dumps(_scan))


def start_scan(destination: str) -> dict:
    dest = config.clean_dest(destination) or config.DEFAULT_DEST
    root = config.safe_join(config.LIBRARY_ROOT, dest)
    with _scan_lock:
        if _scan.get("state") == "running":
            return {"ok": False, "message": "A scan is already running."}
        _scan.clear()
        _scan.update({"state": "running", "destination": dest, "started": int(time.time()),
                      "checked": 0, "total": 0, "findings": [], "skipped_no_id": 0,
                      "failed": 0, "attribution": ATTRIBUTION})
    threading.Thread(target=_run_scan, args=(root, dest), daemon=True).start()
    return {"ok": True}


def _run_scan(root: Path, dest: str) -> None:
    try:
        files = sorted(p for p in root.rglob("*")
                       if p.is_file() and p.suffix.lower() in presets.MEDIA_EXTS
                       and config.STAGING_DIR not in p.parents
                       and not any(part.startswith(".") for part in p.relative_to(root).parts))
        rows = []
        for p in files:
            info = _info(p)
            ext = info.get("extractor", "")
            if info.get("id") and (not ext or "youtube" in ext):
                rows.append((p, info))
        with _scan_lock:
            _scan["total"] = len(rows)
            _scan["skipped_no_id"] = len(files) - len(rows)
        # Batch lookups so the progress counter moves.
        for i in range(0, len(rows), 20):
            batch = rows[i:i + 20]
            res = lookup([info["id"] for _, info in batch])
            found = []
            for p, info in batch:
                segs = res["segments"].get(info["id"])
                if not segs:
                    continue
                rel = str(p.relative_to(config.LIBRARY_ROOT))
                try:
                    chs, dur = _probe(p)
                except Exception:  # noqa: BLE001
                    chs, dur = [], 0.0
                found.append({
                    "path": rel, "show": p.relative_to(root).parts[0] if len(p.relative_to(root).parts) > 1 else "",
                    "name": p.name, "video_id": info["id"], "title": info.get("title", ""),
                    "segments": segs, "chapters": len(chs), "duration": dur,
                    "marked": any(c["title"].startswith(MARK_PREFIX) for c in chs),
                    "can_undo": _backup_path(rel).is_file(),
                    "markable": p.suffix.lower() in MARKABLE_EXTS,
                    "split_songs": sum(1 for c in chs for s in segs
                                       if c["start"] < s["start"] and c["end"] > s["end"]),
                })
            with _scan_lock:
                _scan["checked"] += len(batch)
                _scan["failed"] += len(res["failed"])
                _scan["findings"] += found
        with _scan_lock:
            _scan["state"] = "done"
            _scan["finished"] = int(time.time())
            _save_scan(_scan)
    except Exception as e:  # noqa: BLE001
        with _scan_lock:
            _scan["state"] = "error"
            _scan["error"] = str(e)
            _save_scan(_scan)
