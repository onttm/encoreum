#!/usr/bin/env python3
"""
watch-channel.py
=========================================================================
Watch a YouTube channel and catch scheduled live streams — the machine-readable
equivalent of clicking the notification bell.

WHY THIS EXISTS
    YouTube's bell fires a push to the app and to email. Neither is data you can
    act on from a server, and there is no API for "a channel I follow went live".
    But the same information is available anonymously:

        1. Every channel publishes a PUBLIC Atom feed:
             https://www.youtube.com/feeds/videos.xml?channel_id=UC...
           No API key, no OAuth, no quota, no account. A scheduled stream appears
           in it as soon as it is announced.

        2. yt-dlp reports `live_status` (is_upcoming / is_live / post_live /
           was_live / not_live) and `release_timestamp` for any public video.

    So the whole flow — notice, schedule, record — needs NO credentials at all.
    That matters: authenticating yt-dlp means persisting a Google *session*
    cookie, which is account-wide and gets accounts banned for automation. The
    only safe amount of YouTube auth here is none.

    The feed is also a plain HTTP GET, so it keeps working when yt-dlp itself is
    being throttled by YouTube's anti-bot measures.

WHAT IT DOES
    Reads a channel's feed, remembers what it has already seen, and reports any
    entry that is upcoming or live. With --queue it hands the URL straight to a
    running Encoreum instance, which then waits for the stream and records it
    from the start.

USAGE
    # what's coming up on a channel
    ./watch-channel.py https://www.youtube.com/@AustinCityLimitsTV

    # keep watching, and queue anything scheduled into Encoreum
    ./watch-channel.py @AustinCityLimitsTV --queue --show "Austin City Limits" \
        --loop 15m

    # from cron (recommended): one pass, exits immediately
    ./watch-channel.py UCLrFzxcwT97ROXn3JAiDIqQ --queue --show "ACL" --once

    ./watch-channel.py <channel> --json        # machine-readable
    ./watch-channel.py <channel> --all         # include ordinary uploads

Standalone: Python 3 stdlib + the yt-dlp binary. No API key, no account.
=========================================================================
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

NS = {"a": "http://www.w3.org/2005/Atom",
      "yt": "http://www.youtube.com/xml/schemas/2015",
      "media": "http://search.yahoo.com/mrss/"}

FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")

# live_status values that mean "there is something to capture"
ACTIONABLE = ("is_upcoming", "is_live")


def find_ytdlp() -> str:
    for cand in (os.environ.get("YTDLP_BIN"),
                 "/config/venv/bin/yt-dlp",
                 shutil.which("yt-dlp")):
        if cand and os.path.exists(cand):
            return cand
    sys.exit("yt-dlp not found. Set YTDLP_BIN or install yt-dlp.")


class RateLimited(Exception):
    """YouTube returned 429."""


def http_get(url: str, timeout: int = 30, cond: dict | None = None) -> tuple[bytes | None, dict]:
    """
    GET with CONDITIONAL request support.

    Returns (body, validators). body is None on a 304 Not Modified, which is the
    whole point: a feed that has not changed costs ~300 bytes and no parsing.
    The RSS endpoint has no published rate limit, so being a well-behaved HTTP
    client is the actual protection — send If-None-Match/If-Modified-Since and
    honour 304s rather than re-downloading 28KB every pass.
    """
    headers = {
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9",
        # YouTube 303-redirects EU/consent traffic; this short-circuits it.
        "Cookie": "CONSENT=YES+1",
    }
    if cond:
        if cond.get("etag"):
            headers["If-None-Match"] = cond["etag"]
        if cond.get("last_modified"):
            headers["If-Modified-Since"] = cond["last_modified"]
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), {"etag": r.headers.get("ETag"),
                              "last_modified": r.headers.get("Last-Modified")}
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, cond or {}
        if e.code == 429:
            raise RateLimited() from e
        raise


def resolve_channel_id(target: str) -> str | None:
    """Accept a channel_id, an @handle, or any channel URL."""
    t = target.strip()
    if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", t):
        return t
    m = re.search(r"channel/(UC[A-Za-z0-9_-]{22})", t)
    if m:
        return m.group(1)
    if t.startswith("@"):
        t = f"https://www.youtube.com/{t}"
    if not t.startswith("http"):
        t = f"https://www.youtube.com/@{t}"
    try:
        body, _ = http_get(t, timeout=45)
        html = (body or b"").decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        print(f"could not fetch {t}: {e}", file=sys.stderr)
        return None
    for pat in (r'"(?:externalId|browseId)":"(UC[A-Za-z0-9_-]{22})"',
                r'channel/(UC[A-Za-z0-9_-]{22})'):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


def read_feed(cid: str, state: dict | None = None) -> list[dict] | None:
    """Returns None when the feed is unchanged (HTTP 304) — nothing to do."""
    cond = (state or {}).get("http", {})
    try:
        raw, validators = http_get(FEED.format(cid=cid), cond=cond)
    except RateLimited:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"feed fetch failed: {e}", file=sys.stderr)
        return []
    if state is not None:
        state["http"] = {k: v for k, v in validators.items() if v}
    if raw is None:
        return None
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"feed parse failed: {e}", file=sys.stderr)
        return []
    out = []
    for e in root.findall("a:entry", NS):
        vid = e.find("yt:videoId", NS)
        title = e.find("a:title", NS)
        pub = e.find("a:published", NS)
        if vid is None or not vid.text:
            continue
        out.append({
            "id": vid.text,
            "title": (title.text if title is not None else "") or "",
            "published": (pub.text if pub is not None else "") or "",
            "url": f"https://www.youtube.com/watch?v={vid.text}",
        })
    return out


def live_state(ytdlp: str, url: str) -> tuple[str, int | None]:
    """(live_status, release_timestamp). --ignore-no-formats-error is REQUIRED:
    an upcoming stream has no formats yet and yt-dlp errors out without it."""
    try:
        r = subprocess.run(
            [ytdlp, "--no-warnings", "--skip-download", "--ignore-no-formats-error",
             "--print", "%(live_status)s|%(release_timestamp)s", url],
            capture_output=True, text=True, timeout=90)
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


def queue_job(api: str, url: str, show: str, dest: str, timeout: int = 30) -> bool:
    """POST to a running Encoreum. `live=1` makes the app wait for the stream
    and capture it from the start."""
    import urllib.parse
    body = urllib.parse.urlencode({
        "url": url, "show": show, "destination": dest,
        "naming": "datecode", "quality": "max",
        "live": "1", "run_nfo": "1", "run_artwork": "1",
        "genres": "Music, Concert",
    }).encode()
    req = urllib.request.Request(
        api.rstrip("/") + "/jobs", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status in (200, 303)
    except urllib.error.HTTPError as e:
        return e.code in (200, 303)          # 303 redirect = job created
    except Exception as e:  # noqa: BLE001
        print(f"queue failed: {e}", file=sys.stderr)
        return False


def load_state(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def save_state(p: Path, s: dict) -> None:
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(s, indent=1))
    except OSError as e:
        print(f"could not write state {p}: {e}", file=sys.stderr)


def parse_interval(s: str) -> int:
    m = re.fullmatch(r"(\d+)([smh]?)", s.strip(), re.I)
    if not m:
        raise argparse.ArgumentTypeError("use e.g. 900, 15m, 1h")
    n, unit = int(m.group(1)), (m.group(2) or "s").lower()
    return n * {"s": 1, "m": 60, "h": 3600}[unit]


def one_pass(args, ytdlp: str, cid: str, state: dict) -> list[dict] | None:
    """Returns None if the feed was unchanged (304) — caller reports 'no change'
    without spending a single yt-dlp request."""
    entries = read_feed(cid, state)
    if entries is None:
        # Feed unchanged. Still re-check anything upcoming, because an entry's
        # live_status flips to is_live WITHOUT the feed changing.
        pending = [vid for vid, v in state.get("seen", {}).items()
                   if v.get("status") == "is_upcoming"]
        if not pending:
            return None
        entries = [{"id": v, "title": state["seen"][v].get("title", ""),
                    "published": "",
                    "url": f"https://www.youtube.com/watch?v={v}"} for v in pending]
    if not entries:
        return []
    seen: dict = state.setdefault("seen", {})
    found = []
    for e in entries:
        prev = seen.get(e["id"], {})
        # Re-check anything still upcoming — that is the whole point; its status
        # flips to is_live at showtime. Skip only settled, already-seen entries.
        if prev.get("status") in ("was_live", "not_live", "queued") and not args.recheck:
            continue
        st, rel = live_state(ytdlp, e["url"])
        e["live_status"] = st
        e["release_timestamp"] = rel
        e["scheduled"] = (datetime.fromtimestamp(rel).strftime("%Y-%m-%d %H:%M")
                          if rel else "")
        if st in ACTIONABLE or args.all:
            found.append(e)
        seen[e["id"]] = {"status": st, "title": e["title"], "checked": int(time.time())}
    return found


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("channel", help="channel_id, @handle, or channel URL")
    ap.add_argument("--state", default="", help="state file (default: alongside config)")
    ap.add_argument("--queue", action="store_true",
                    help="POST actionable streams to a running Encoreum")
    ap.add_argument("--api", default=(os.environ.get("ENCOREUM_API") or os.environ.get("ORPHEUM_API")
                                             or "http://localhost:8681"))
    ap.add_argument("--show", default="", help="Plex show name (required with --queue)")
    ap.add_argument("--dest", default="concerts", help="library subfolder")
    ap.add_argument("--queue-within", type=parse_interval, default=parse_interval("8h"),
                    metavar="INTERVAL",
                    help="only queue a scheduled stream once it is this close "
                         "(default 8h). Streams get announced days ahead; parking "
                         "a job for that long ties up a worker for nothing.")
    ap.add_argument("--loop", type=parse_interval, default=0, metavar="INTERVAL",
                    help="keep watching every INTERVAL (e.g. 15m); default single pass")
    ap.add_argument("--once", action="store_true", help="single pass (default)")
    ap.add_argument("--all", action="store_true",
                    help="report ordinary uploads too, not just live/upcoming")
    ap.add_argument("--recheck", action="store_true",
                    help="re-check entries already settled as VOD/upload")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.queue and not args.show:
        sys.exit("--queue needs --show (the Plex series name to file it under)")

    ytdlp = find_ytdlp()
    cid = resolve_channel_id(args.channel)
    if not cid:
        sys.exit(f"could not resolve a channel id from {args.channel!r}")

    state_path = Path(args.state) if args.state else Path(
        os.environ.get("CONFIG_DIR", str(Path.home() / ".cache/encoreum"))
    ) / f"watch-{cid}.json"
    state = load_state(state_path)

    if not args.json:
        print(f"channel : {cid}")
        print(f"feed    : {FEED.format(cid=cid)}")
        print(f"state   : {state_path}")
        print()

    backoff = 0
    while True:
        try:
            found = one_pass(args, ytdlp, cid, state)
            backoff = 0
        except RateLimited:
            # YouTube 429. Published guidance is: back off hard and wait it out —
            # tripped limits typically clear within the hour. Exponential to a
            # 4h ceiling, mirroring what well-behaved feed readers do.
            backoff = min(4 * 3600, (backoff * 2) or 900)
            print(f"HTTP 429 from YouTube — backing off {backoff // 60} min.",
                  file=sys.stderr)
            if not args.loop or args.once:
                return 2
            time.sleep(backoff)
            continue

        if found is None:
            if not args.json:
                print(f"[{datetime.now():%H:%M:%S}] feed unchanged (304), "
                      "nothing pending.")
            found = []
        actionable = [f for f in found if f.get("live_status") in ACTIONABLE]

        if args.json:
            print(json.dumps(found, indent=2))
        else:
            stamp = datetime.now().strftime("%H:%M:%S")
            if not found:
                print(f"[{stamp}] nothing new.")
            for f in found:
                mark = {"is_upcoming": "SCHEDULED", "is_live": "LIVE NOW"}.get(
                    f["live_status"], f["live_status"] or "?")
                when = f" @ {f['scheduled']}" if f["scheduled"] else ""
                print(f"[{stamp}] {mark:<10}{when}  {f['title'][:56]}")
                print(f"           {f['url']}")

        if args.queue:
            for f in actionable:
                if state.get("seen", {}).get(f["id"], {}).get("status") == "queued":
                    continue
                rel = f.get("release_timestamp")
                if (f.get("live_status") == "is_upcoming" and rel
                        and rel - time.time() > args.queue_within):
                    if not args.json:
                        hrs = (rel - time.time()) / 3600
                        print(f"           -> not queuing yet ({hrs:.1f}h away; "
                              f"queues within {args.queue_within // 3600}h)")
                    continue
                ok = queue_job(args.api, f["url"], args.show, args.dest)
                if not args.json:
                    print(f"           -> {'queued' if ok else 'QUEUE FAILED'} "
                          f"to {args.api}")
                if ok:
                    state.setdefault("seen", {})[f["id"]]["status"] = "queued"

        save_state(state_path, state)

        if not args.loop or args.once:
            return 0

        # Adaptive cadence. Tighten as a scheduled start approaches, but never
        # go below ~2 min: streamlink records with --hls-live-restart, so a
        # capture that begins a minute late STILL gets the stream from its
        # beginning. There is nothing to gain from hammering the API to catch
        # the exact instant, and feed propagation lags a few minutes anyway.
        delay = args.loop
        soonest = min((f["release_timestamp"] for f in actionable
                       if f.get("release_timestamp")), default=None)
        if soonest:
            remaining = soonest - time.time()
            if remaining < 900:
                delay = min(delay, 120)
            elif remaining < 7200:
                delay = min(delay, 300)
        # Jitter: perfectly regular intervals are themselves a bot signal.
        delay = int(delay * (0.85 + 0.3 * (os.urandom(1)[0] / 255)))
        if not args.json:
            print(f"           next check in {delay // 60}m{delay % 60:02d}s")
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
