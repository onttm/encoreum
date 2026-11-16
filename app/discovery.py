"""
discovery.py -- find upcoming live concerts on watched channels.

Runs on the APScheduler instance already living in main.py. One pass:

    for each approved source:
        skip entirely if the CDN copy is still fresh     <- Cache-Control max-age
        else GET the channel's public feed
        upsert anything new into events                  <- thumb + description free
    then, for a bounded batch of unsettled events:
        ask yt-dlp the ONE question the feed cannot:     <- is it a stream, and when?
        live_status + release_timestamp

WHY IT IS SPLIT THAT WAY
    The public Atom feed carries title, video id, description, thumbnail and view
    count — verified against a real entry. So the browse UI costs nothing to
    populate. yt-dlp is the expensive side (it hits the InnerTube player API,
    where bot detection lives), so it is called only for entries that are not
    already settled, and only up to `status_budget` per pass.

NO CREDENTIALS ANYWHERE
    The feed needs no key, quota or account. Authenticating yt-dlp would mean
    persisting a Google *session* cookie, which is account-wide rather than
    YouTube-scoped and gets accounts banned for automation. Deliberately absent.

RATE DISCIPLINE
    Neither endpoint publishes a limit, so the rules are behavioural: honour
    Cache-Control, jitter the schedule (regular timing is itself a bot signal),
    and back off on 429. A tripped limit takes about an hour to clear — long
    enough to lose a broadcast.

    MEASURED, not assumed: the feed sends NO ETag and NO Last-Modified, so
    conditional requests cannot produce a 304 here. It does send
    `Cache-Control: public, max-age=900`, which is why freshness-skipping is the
    real saving and why polling faster than 15 minutes returns identical bytes.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from . import catalog, config
from . import secrets

NS = {"a": "http://www.w3.org/2005/Atom",
      "yt": "http://www.youtube.com/xml/schemas/2015",
      "media": "http://search.yahoo.com/mrss/"}
FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")

_poll_lock = threading.Lock()
_backoff_until = 0.0          # epoch; set when YouTube returns 429
_backoff_step = 0             # consecutive 429s, for escalating backoff

# Escalating, because a flat retry interval walks straight back into the same
# wall. Documented behaviour and actual behaviour had drifted apart here: this
# file claimed an exponential 4h ceiling while the code applied a flat hour and
# retried into the limit indefinitely. Limits are pinned in app/ratelimit.py.
_BACKOFF_STEPS = (3600, 7200, 14400)      # 1h, 2h, 4h ceiling

# Reuse the standalone script's classifier so the CLI tool and the service agree
# on what counts as a concert. Single source of truth, no duplicated regexes.
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load_classifier():
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_analyze", _SCRIPTS / "analyze-channel.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.classify
    except Exception:  # noqa: BLE001
        return None


_classify = _load_classifier()


class RateLimited(Exception):
    """YouTube returned 429."""


def _parse_max_age(cache_control: str | None) -> int | None:
    if not cache_control:
        return None
    m = re.search(r"max-age=(\d+)", cache_control)
    return int(m.group(1)) if m else None


def _http_get(url: str, timeout: int = 30,
              etag: str | None = None, last_modified: str | None = None):
    """Conditional GET. Returns (body|None, etag, last_modified, max_age).

    body is None on 304. NOTE (measured 2026-08-21): YouTube's feed endpoint
    sends neither ETag nor Last-Modified, so a 304 will not happen there — the
    conditional headers are sent anyway in case that ever changes, and the
    freshness saving comes from Cache-Control instead.
    """
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
               "Cookie": "CONSENT=YES+1"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return (r.read(), r.headers.get("ETag"), r.headers.get("Last-Modified"),
                    _parse_max_age(r.headers.get("Cache-Control")))
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, etag, last_modified, None
        if e.code == 429:
            raise RateLimited() from e
        raise


def resolve_channel(target: str) -> tuple[str, str] | None:
    """(channel_id, channel_title) from a channel_id, @handle or URL."""
    t = (target or "").strip()
    cid = None
    if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", t):
        cid = t
    else:
        m = re.search(r"channel/(UC[A-Za-z0-9_-]{22})", t)
        if m:
            cid = m.group(1)
    if not cid:
        url = t if t.startswith("http") else (
            f"https://www.youtube.com/{t}" if t.startswith("@")
            else f"https://www.youtube.com/@{t}")
        try:
            body, _, _, _ = _http_get(url, timeout=45)
        except Exception:  # noqa: BLE001
            return None
        html = (body or b"").decode("utf-8", "replace")
        for pat in (r'"(?:externalId|browseId)":"(UC[A-Za-z0-9_-]{22})"',
                    r'channel/(UC[A-Za-z0-9_-]{22})'):
            m = re.search(pat, html)
            if m:
                cid = m.group(1)
                break
    if not cid:
        return None
    # Title comes from the feed itself — one request, already needed.
    try:
        body, _, _, _ = _http_get(FEED.format(cid=cid))
        root = ET.fromstring(body or b"")
        title = (root.find("a:title", NS).text or cid)
    except Exception:  # noqa: BLE001
        title = cid
    return cid, title


def parse_feed(raw: bytes) -> list[dict]:
    """Feed entries INCLUDING thumbnail + description — both are free here."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    out = []
    for e in root.findall("a:entry", NS):
        vid = e.find("yt:videoId", NS)
        if vid is None or not vid.text:
            continue
        g = e.find("media:group", NS)
        thumb = g.find("media:thumbnail", NS) if g is not None else None
        desc = g.find("media:description", NS) if g is not None else None
        title = e.find("a:title", NS)
        pub = e.find("a:published", NS)
        out.append({
            "platform_id": vid.text,
            "url": f"https://www.youtube.com/watch?v={vid.text}",
            "title": (title.text if title is not None else "") or "",
            "description": (desc.text if desc is not None and desc.text else "")[:4000],
            "thumbnail_url": thumb.get("url") if thumb is not None else "",
            "published_at": (pub.text if pub is not None else "") or "",
        })
    return out


def live_state(url: str, timeout: int = 90) -> tuple[str, int | None]:
    """(live_status, release_timestamp) — the one question the feed cannot answer.

    --ignore-no-formats-error is REQUIRED: an upcoming stream has no formats yet
    and yt-dlp exits non-zero without it.
    """
    ytdlp = os.environ.get("YTDLP_BIN") or str(config.YTDLP_BIN)
    try:
        r = subprocess.run(
            [ytdlp, "--no-warnings", "--skip-download", "--ignore-no-formats-error",
             "--print", "%(live_status)s|%(release_timestamp)s", url],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return "", None
    line = next((l.strip() for l in reversed((r.stdout or "").splitlines())
                 if "|" in l), "")
    if not line:
        return "", None
    st, _, ts = line.partition("|")
    st = st.strip()
    try:
        rel = int(ts.strip())
    except (TypeError, ValueError):
        rel = None
    return (st if st and st != "NA" else ""), rel


def poll_once(status_budget: int = 25) -> dict:
    """One discovery pass. Returns a small summary dict for logging/inspection."""
    global _backoff_until, _backoff_step
    import time as _t

    if not _poll_lock.acquire(blocking=False):
        return {"skipped": "already running"}
    try:
        now = _t.time()
        if now < _backoff_until:
            return {"skipped": f"backoff {int(_backoff_until - now)}s"}

        summary = {"sources": 0, "cached": 0, "unchanged": 0, "new_events": 0,
                   "checked": 0, "upcoming": 0, "live": 0, "errors": 0}

        for src in catalog.list_sources(approved_only=True):
            summary["sources"] += 1
            # The CDN serves byte-identical content until max-age expires, so a
            # request inside that window is pure waste. This is what actually
            # keeps the poll cheap — NOT 304s, which this endpoint never sends.
            if src.get("fresh_until") and now < src["fresh_until"]:
                summary["cached"] = summary.get("cached", 0) + 1
                continue
            try:
                body, etag, lastmod, max_age = _http_get(
                    FEED.format(cid=src["channel_id"]),
                    etag=src.get("etag"), last_modified=src.get("last_modified"))
            except RateLimited:
                # A tripped limit clears in roughly an hour, but a SECOND trip
                # means an hour was not enough, so the wait escalates rather
                # than repeating.
                wait = _BACKOFF_STEPS[min(_backoff_step, len(_BACKOFF_STEPS) - 1)]
                _backoff_step += 1
                _backoff_until = _t.time() + wait
                summary["errors"] += 1
                return {**summary, "rate_limited": True,
                        "backoff_seconds": wait}
            except Exception:  # noqa: BLE001
                catalog.record_poll(src["id"], src.get("etag"),
                                    src.get("last_modified"), failed=True)
                summary["errors"] += 1
                continue

            # A fetch that came back clean means we are no longer being
            # limited, so the escalation resets. Without this the wait would
            # ratchet to 4h permanently after a single bad afternoon.
            _backoff_step = 0
            catalog.record_poll(src["id"], etag, lastmod,
                                fresh_until=int(_t.time() + max_age) if max_age else None)
            if body is None:
                summary["unchanged"] += 1
                continue
            for item in parse_feed(body):
                before = catalog.get_event_id(src["id"], item["platform_id"])
                catalog.upsert_event(src["id"], **item)
                if not before:
                    summary["new_events"] += 1

        # Now spend a bounded number of yt-dlp calls on unsettled entries only.
        for ev in catalog.events_needing_check(limit=status_budget):
            st, rel = live_state(ev["url"])
            cls = ""
            if _classify:
                try:
                    cls = _classify(ev["title"], None, 0)
                except Exception:  # noqa: BLE001
                    cls = ""
            catalog.update_event_status(ev["id"], st, rel, cls)
            summary["checked"] += 1
            if st == "is_upcoming":
                summary["upcoming"] += 1
            elif st == "is_live":
                summary["live"] += 1

        # Resolve any newly-ingested festival sets in the same pass.
        try:
            summary["enriched"] = enrich_segments()
        except Exception:  # noqa: BLE001
            summary["enriched"] = {"error": True}

        return summary
    finally:
        _poll_lock.release()


def add_channel(target: str, show: str = "", destination: str = "concerts",
                ephemeral: bool = True, auto_record: bool = False,
                added_by: str = "") -> dict:
    """Resolve a channel and register it as a source. Idempotent."""
    got = resolve_channel(target)
    if not got:
        return {"ok": False, "error": f"could not resolve a channel from {target!r}"}
    cid, title = got
    sid = catalog.add_source("youtube", cid, title, show or title,
                             destination, ephemeral, auto_record, added_by)
    return {"ok": bool(sid), "source_id": sid, "channel_id": cid, "title": title}


# --- festival artist enrichment ---------------------------------------------
def resolve_artist(name: str, timeout: int = 120) -> dict:
    """Resolve one artist via scripts/artist-lookup.py.

    Shelling out to the standalone script rather than importing it is the same
    pattern pipeline.py uses, and it keeps ONE implementation of matching — the
    CLI and the service can never disagree about who "Levity" is, including
    about any human correction in artist-overrides.json.
    """
    script = _SCRIPTS / "artist-lookup.py"
    if not script.exists():
        return {}
    try:
        r = subprocess.run(["python3", str(script), name, "--json"],
                           capture_output=True, text=True, timeout=timeout)
        data = json.loads(r.stdout or "[]")
        return data[0] if data else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


def _library_note(res: dict, library_name: str = "the Plex server") -> str:
    """The one line that drives the decision, precomputed at ingest."""
    plex = res.get("plex") or {}
    aud, vid = plex.get("audio") or [], plex.get("video") or []
    albums = sum(1 for i in aud if i.get("type") == "album")
    tracks = sum(1 for i in aud if i.get("type") == "track")
    vids = len(vid)
    a = (f"{albums} album{'s' if albums != 1 else ''}" if albums
         else (f"{tracks} track{'s' if tracks != 1 else ''}" if tracks else ""))
    if aud and not vid:
        return f"{library_name} has {a}, no live footage"
    if not aud and not vid:
        return f"{library_name} has nothing for this artist"
    if vid and not aud:
        return f"{library_name} has {vids} video item{'s' if vids != 1 else ''}, no albums"
    return f"{library_name} has {a} and {vids} video item{'s' if vids != 1 else ''}"


def enrich_segments(budget: int = 20) -> dict:
    """Resolve artist + genre + library holdings for newly ingested sets.

    Bounded per pass: each resolution is a Lidarr/MusicBrainz call plus two Plex
    calls, and a festival schedule can be 100+ sets. Runs from the same poller.
    """
    done = {"resolved": 0, "unmatched": 0}
    for seg in catalog.segments_unenriched(limit=budget):
        res = resolve_artist(seg["artist"])
        if not res or not res.get("mbid"):
            # Mark it attempted so an unmatchable name is not retried forever.
            catalog.enrich_segment(seg["id"], artist_mbid="unmatched",
                                   library_note="could not identify this artist")
            done["unmatched"] += 1
            continue
        catalog.enrich_segment(seg["id"], artist_mbid=res["mbid"],
                               genres=res.get("genres") or [],
                               library_note=_library_note(res))
        done["resolved"] += 1
    return done


# --- integration health ------------------------------------------------------
def check_integrations() -> dict:
    """Test each optional integration and say plainly what is wrong.

    Configuration lives in env (credentials do not belong in a settings DB), so
    this is the other half of that choice: env for setup, a live check for
    verification. "Did I paste the right key" should have a definite answer, not
    a silent degradation to 'could not identify this artist'.

    Distinguishes NOT CONFIGURED from CONFIGURED-BUT-BROKEN, because they need
    completely different fixes.
    """
    import urllib.error as _ue

    out = {}

    # --- Lidarr ---
    url = os.environ.get("LIDARR_URL", "http://172.17.0.1:8686").rstrip("/")
    key = secrets.read("LIDARR_API_KEY")
    d = {"name": "Lidarr", "purpose": "artist identity + genre",
         "url": url, "configured": bool(key)}
    if not key:
        d.update(ok=False, status="not configured",
                 hint="put the key in the encoreum_lidarr_api_key docker secret "
                      "(Lidarr > Settings > General > API Key)")
    else:
        try:
            req = urllib.request.Request(f"{url}/api/v1/system/status",
                                         headers={"X-Api-Key": key})
            with urllib.request.urlopen(req, timeout=15) as r:
                info = json.loads(r.read())
            d.update(ok=True, status="ok",
                     detail=f"Lidarr {info.get('version', '?')}")
        except _ue.HTTPError as e:
            d.update(ok=False, status=f"HTTP {e.code}",
                     hint=("API key rejected — check it in Lidarr > Settings > General"
                           if e.code in (401, 403) else "unexpected response"))
        except Exception as e:  # noqa: BLE001
            d.update(ok=False, status="unreachable",
                     hint=(f"cannot reach {url}. From a container the host is "
                           "172.17.0.1, not localhost."), error=str(e)[:120])
    out["lidarr"] = d

    # --- Plex ---
    purl = os.environ.get("PLEX_URL", "http://172.17.0.1:32400").rstrip("/")
    tok = secrets.read("PLEX_TOKEN")
    p = {"name": "Plex", "purpose": "what the library already holds",
         "url": purl, "configured": bool(tok)}
    if not tok:
        p.update(ok=False, status="not configured",
                 hint="put the token in the encoreum_plex_token docker secret "
                      "(Preferences.xml -> PlexOnlineToken)")
    else:
        try:
            q = urllib.parse.urlencode({"X-Plex-Token": tok})
            with urllib.request.urlopen(f"{purl}/library/sections?{q}", timeout=15) as r:
                root = ET.fromstring(r.read())
            secs = [{"title": x.get("title"), "type": x.get("type")}
                    for x in root.findall("Directory")]
            audio = [s["title"] for s in secs if s["type"] == "artist"]
            video = [s["title"] for s in secs if s["type"] in ("movie", "show")]
            p.update(ok=True, status="ok",
                     detail=f"{len(secs)} libraries", audio_libraries=audio,
                     video_libraries=video)
            if not audio:
                p["warning"] = ("no music libraries — 'has N albums' will always "
                                "read as nothing")
        except _ue.HTTPError as e:
            p.update(ok=False, status=f"HTTP {e.code}",
                     hint=("token rejected — Plex tokens expire when the account "
                           "signs out everywhere" if e.code in (401, 403)
                           else "unexpected response"))
        except Exception as e:  # noqa: BLE001
            p.update(ok=False, status="unreachable",
                     hint=f"cannot reach {purl}. From a container use 172.17.0.1.",
                     error=str(e)[:120])
    out["plex"] = p

    # --- yt-dlp (not optional; discovery depends on it) ---
    ybin = os.environ.get("YTDLP_BIN") or str(config.YTDLP_BIN)
    y = {"name": "yt-dlp", "purpose": "stream status + downloads", "url": ybin,
         "configured": True}
    try:
        r = subprocess.run([ybin, "--version"], capture_output=True, text=True,
                           timeout=30)
        v = (r.stdout or "").strip()
        y.update(ok=bool(v), status="ok" if v else "no version output", detail=v)
    except Exception as e:  # noqa: BLE001
        y.update(ok=False, status="not runnable", error=str(e)[:120])
    out["ytdlp"] = y

    out["summary"] = {
        "all_ok": all(v.get("ok") for k, v in out.items() if k != "summary"),
        "degraded": [k for k, v in out.items()
                     if k != "summary" and not v.get("ok")],
    }
    return out
