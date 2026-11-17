"""
streamscan.py -- find a YouTube channel's live streams (live now, upcoming,
past) for the Streams page timeline. Read-only: it never downloads anything;
the page's Record/Schedule/Download buttons open the normal job form.

WHERE THE DATA COMES FROM (measured 2026-11-16)
    1. The channel's **Live tab** (`/streams`), one flat yt-dlp call: up to
       hundreds of entries with id, title, thumbnail, duration, views and
       live_status (is_live / is_upcoming / was_live) -- but NO dates.
       Some channels have no Live tab at all (Live Show TV: "This
       channel does not have a streams tab"); their streams are mixed into
       ordinary uploads.
    2. The channel's **RSS feed**: real publish dates for the newest ~15
       videos, free (same feed discovery.py polls).
    3. **Per-video lookups** for what's still missing: an upcoming stream's
       start time, an older stream's date, or -- for a channel with no Live
       tab -- whether an upload was live at all. Each is an InnerTube player
       call, so each goes through ratelimit.acquire("youtube").

    Undated past streams are placed on the timeline by their ORDER (the Live
    tab is newest-first) between the nearest dated neighbours and flagged
    `approx`, so the timeline is useful immediately and sharpens as dates
    arrive. "Scan more" looks up the next batch.

STATE
    One background scan at a time, results cached per channel in
    /config/streamscan/<channel_id>.json so revisiting a channel is instant.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, discovery, ratelimit

CACHE_DIR = config.CONFIG_DIR / "streamscan"
LIVE_TAB_MAX = 400          # entries read from a Live tab in one flat call
UPLOADS_FIRST = 50          # uploads checked automatically when there's no Live tab
DATES_FIRST = 25            # past streams dated automatically after a Live-tab read
BATCH = 50                  # "Scan more" step

_lock = threading.Lock()
_state: dict = {"state": "idle"}


def _ytdlp() -> str:
    return os.environ.get("YTDLP_BIN") or str(config.YTDLP_BIN)


def _cache_path(cid: str) -> Path:
    return CACHE_DIR / f"{cid}.json"


def _save(s: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _cache_path(s["channel_id"]).with_suffix(".tmp")
        tmp.write_text(json.dumps(s), encoding="utf-8")
        os.replace(tmp, _cache_path(s["channel_id"]))
    except (OSError, KeyError):
        pass


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def status() -> dict:
    with _lock:
        out = json.loads(json.dumps(_state))
    if out.get("streams"):
        out["streams"] = place(out["streams"])
    return out


def channels() -> list:
    """Channels scanned before (newest first), for quick picks on the page."""
    out = []
    for p in CACHE_DIR.glob("*.json"):
        try:
            s = json.loads(p.read_text(encoding="utf-8"))
            out.append({"channel_id": s["channel_id"], "channel": s.get("channel", ""),
                        "finished": s.get("finished", 0), "count": len(s.get("streams", []))})
        except (OSError, ValueError, KeyError):
            continue
    return sorted(out, key=lambda c: -c["finished"])


def load(channel_id: str) -> dict | None:
    try:
        s = json.loads(_cache_path(channel_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    s["streams"] = place(s.get("streams", []))
    return s


# --- yt-dlp calls ---------------------------------------------------------------

def _flat(url: str, limit: int) -> tuple[list, str]:
    """Flat listing of a channel tab. Returns (entries, error_text)."""
    ok, why = ratelimit.acquire("youtube")
    if not ok:
        return [], why
    try:
        r = subprocess.run([_ytdlp(), "--flat-playlist", "--playlist-end", str(limit),
                            "--no-warnings", "-J", url],
                           capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        return [], str(e)
    try:
        data = json.loads(r.stdout or "null")
    except ValueError:
        data = None
    if not data:
        err = (r.stderr or "").strip().splitlines()
        return [], (err[-1] if err else "no data")
    return [e for e in (data.get("entries") or []) if e and e.get("id")], ""


def _details(vid: str) -> dict | None:
    """live_status + dates for one video (one InnerTube call). None = skipped."""
    ok, _ = ratelimit.acquire("youtube")
    if not ok:
        return None
    fmt = "%(live_status)s|%(release_timestamp)s|%(timestamp)s|%(upload_date)s|%(duration)s|%(title)s"
    try:
        r = subprocess.run([_ytdlp(), "--no-warnings", "--skip-download",
                            "--ignore-no-formats-error", "--print", fmt,
                            f"https://www.youtube.com/watch?v={vid}"],
                           capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.SubprocessError):
        return {}
    line = next((l for l in reversed((r.stdout or "").splitlines()) if l.count("|") >= 5), "")
    if not line:
        return {}
    st, rel, ts, ud, dur, title = line.split("|", 5)
    num = lambda v: int(float(v)) if re.fullmatch(r"\d+(\.\d+)?", v or "") else None
    when = num(rel) or num(ts)
    if not when and re.fullmatch(r"\d{8}", ud or ""):
        when = int(datetime.strptime(ud, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp())
    return {"live_status": "" if st == "NA" else st, "when": when,
            "duration": num(dur), "title": "" if title == "NA" else title}


def _thumb(e: dict) -> str:
    th = e.get("thumbnails") or []
    if th:
        # Prefer a mid-size one; the timeline shows small cards.
        mids = [t for t in th if (t.get("width") or 0) and 300 <= t["width"] <= 500]
        return (mids or th)[-1].get("url", "")
    return f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg"


# --- placement on the timeline --------------------------------------------------

def place(streams: list) -> list:
    """Give every stream a `t` (epoch) for the timeline.

    Dated ones keep their date. Undated ones (most of a Live tab) sit between
    their nearest dated neighbours by ORDER -- the tab is newest-first -- and
    are flagged approx. Live now = now; upcoming without a time = just ahead.
    """
    now = int(time.time())
    out = [dict(s) for s in streams]
    past = [s for s in out if s["status"] == "was_live"]
    known = [(i, s["when"]) for i, s in enumerate(past) if s.get("when")]
    for i, s in enumerate(past):
        if s.get("when"):
            s["t"], s["approx"] = s["when"], False
            continue
        newer = next(((j, w) for j, w in reversed(known) if j < i), None)
        older = next(((j, w) for j, w in known if j > i), None)
        if newer and older:
            frac = (i - newer[0]) / (older[0] - newer[0])
            s["t"] = int(newer[1] - frac * (newer[1] - older[1]))
        elif newer:          # older than every dated one: step back ~1 week each
            s["t"] = newer[1] - (i - newer[0]) * 7 * 86400
        elif older:          # newer than every dated one: spread evenly up to now
            s["t"] = int(older[1] + (older[0] - i) / (older[0] + 1) * (now - 3600 - older[1]))
        else:
            s["t"] = now - (i + 1) * 7 * 86400
        s["approx"] = True
    for s in out:
        if s["status"] == "is_live":
            s["t"], s["approx"] = now, False
        elif s["status"] == "is_upcoming":
            s["t"] = s.get("when") or now + 3600
            s["approx"] = not s.get("when")
    return out


# --- the scan -------------------------------------------------------------------

def start(target: str, more: bool = False) -> dict:
    with _lock:
        if _state.get("state") == "running":
            return {"ok": False, "message": "A scan is already running."}
    res = discovery.resolve_channel(target)
    if not res:
        return {"ok": False, "message": "Couldn't find that channel. Try its URL, @handle or UC… id."}
    cid, title = res
    prev = load(cid) if more else None
    with _lock:
        _state.clear()
        _state.update(prev or {"channel_id": cid, "channel": title, "streams": [],
                               "mode": "", "uploads_checked": 0, "notes": []})
        _state.update({"state": "running", "phase": "starting", "progress": [0, 0],
                       "started": int(time.time()), "error": ""})
    threading.Thread(target=_run, args=(cid, title, more), daemon=True).start()
    return {"ok": True, "channel_id": cid, "channel": title}


def _rss_dates(cid: str) -> dict:
    try:
        body, _, _, _ = discovery._http_get(discovery.FEED.format(cid=cid))
        out = {}
        for e in discovery.parse_feed(body or b""):
            try:
                out[e["platform_id"]] = int(datetime.fromisoformat(
                    e["published_at"].replace("Z", "+00:00")).timestamp())
            except ValueError:
                pass
        return out
    except Exception:  # noqa: BLE001
        return {}


def _run(cid: str, title: str, more: bool) -> None:
    try:
        base = f"https://www.youtube.com/channel/{cid}"
        streams = {s["id"]: s for s in (_state.get("streams") or [])}
        if not more:
            _set(phase="reading the Live tab")
            entries, err = _flat(base + "/streams", LIVE_TAB_MAX)
            if entries:
                mode = "live_tab"
                for e in entries:
                    st = e.get("live_status")
                    st = st if st in ("is_live", "is_upcoming") else "was_live"
                    streams[e["id"]] = {"id": e["id"], "title": e.get("title") or "",
                                        "status": st, "duration": e.get("duration"),
                                        "views": e.get("view_count"), "thumb": _thumb(e),
                                        "when": None, "source": "live tab"}
            elif "does not have a streams tab" in err.lower() or not err:
                mode = "uploads"
                _set(notes=["This channel has no Live tab, so its recent uploads "
                            "are checked one by one for streams."])
            else:
                raise RuntimeError(f"YouTube didn't return the Live tab: {err}")
            _set(mode=mode)
            dates = _rss_dates(cid)
            for vid, ts in dates.items():
                if vid in streams:
                    streams[vid]["when"] = ts
            _set(streams=list(streams.values()))
        mode = _state.get("mode")

        if mode == "uploads":
            done = _state.get("uploads_checked", 0)
            want = done + (BATCH if more else UPLOADS_FIRST)
            _set(phase="listing uploads")
            ups, err = _flat(base + "/videos", want)
            if err and not ups:
                raise RuntimeError(f"Couldn't list uploads: {err}")
            todo = ups[done:want]
            for n, e in enumerate(todo, 1):
                _set(phase="checking uploads for streams", progress=[n, len(todo)])
                d = _details(e["id"])
                if d is None:
                    _set(notes=_state.get("notes", []) + ["YouTube lookup budget spent for today; stopped early."])
                    break
                if d.get("live_status") in ("is_live", "is_upcoming", "was_live", "post_live"):
                    streams[e["id"]] = {"id": e["id"], "title": d.get("title") or e.get("title") or "",
                                        "status": "was_live" if d["live_status"] == "post_live" else d["live_status"],
                                        "duration": d.get("duration") or e.get("duration"),
                                        "views": e.get("view_count"), "thumb": _thumb(e),
                                        "when": d.get("when"), "source": "uploads"}
                _set(streams=list(streams.values()), uploads_checked=done + n)
        else:
            # Upcoming start times first (they drive the countdowns), then dates
            # for the newest undated past streams.
            need = [s for s in streams.values() if s["status"] == "is_upcoming" and not s.get("when")]
            undated = [s for s in streams.values() if s["status"] == "was_live" and not s.get("when")]
            need += undated[:BATCH if more else DATES_FIRST]
            for n, s in enumerate(need, 1):
                _set(phase="looking up dates", progress=[n, len(need)])
                d = _details(s["id"])
                if d is None:
                    _set(notes=_state.get("notes", []) + ["YouTube lookup budget spent for today; stopped early."])
                    break
                if d.get("when"):
                    s["when"] = d["when"]
                if d.get("live_status") in ("is_live", "is_upcoming", "was_live"):
                    s["status"] = d["live_status"]
                _set(streams=list(streams.values()))

        # Keep the Live tab's newest-first order (placement relies on it);
        # uploads mode is already newest-first by listing order.
        order = {s["id"]: i for i, s in enumerate(_state.get("streams") or [])}
        final = sorted(streams.values(), key=lambda s: order.get(s["id"], 0))
        _set(streams=final, state="done", phase="", finished=int(time.time()),
             remaining_undated=sum(1 for s in final if s["status"] == "was_live" and not s.get("when")))
        with _lock:
            snap = json.loads(json.dumps(_state))
        _save(snap)
    except Exception as e:  # noqa: BLE001
        _set(state="error", error=str(e), phase="")
