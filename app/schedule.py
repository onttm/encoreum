"""
schedule.py -- turn a pasted festival set-time list into segments.

WHY PASTE FIRST
    Festivals publish set times in prose, on their own site, in a graphic, or in
    press coverage — Variety and Rolling Stone both ran full Coachella grids.
    There is no API. Scraping any one of them breaks the week they redesign.

    So the primitive is: paste what you can see, get a parsed schedule back,
    check it, commit it. A scraper for a specific festival can be added later as
    a producer of the SAME text, but the paste path must always work, because it
    is the one that never breaks.

    Parsing is deliberately forgiving about layout and strict about times.

TIME HANDLING — the part that silently ruins recordings
    Set times are WALL-CLOCK in the festival's local timezone. They are stored
    as epoch UTC, so the festival's tz must be supplied. Getting this wrong
    shifts every capture by hours and nothing looks broken until playback.

    Overnight rollover is handled: a schedule that runs 20:00, 21:30, 00:40,
    01:50 crosses midnight, and the small hours belong to the NEXT day. Sorting
    by raw clock time would interleave them catastrophically.

    !! STREAM TIME IS NOT STAGE TIME !!
    Verified for Coachella: some sets air delayed — Kacey Musgraves played the
    site at 3pm but was not viewable until the 4pm streams began. ALWAYS ingest
    the STREAMING schedule, never the on-site lineup. Using the lineup records
    the wrong hour, and the file looks fine until someone plays it.

ACCEPTED SHAPES (all seen in the wild)
    19:30 Four Tet
    19:30 - 20:30  Four Tet
    7:30 PM Four Tet
    Four Tet 19:30
    Four Tet — 7:30pm-8:30pm
    19:30–20:30 · Four Tet
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# A clock time: 19:30, 7:30 PM, 7pm
_T = r"(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?"
# time [- time] then artist, or artist then time [- time]
_LEADING = re.compile(rf"^\s*{_T}\s*(?:[-–—]\s*{_T})?\s*[·:\|\-–—]?\s+(.{{2,}}?)\s*$", re.I)
_TRAILING = re.compile(rf"^\s*(.{{2,}}?)\s*[·:\|\-–—]?\s*{_T}\s*(?:[-–—]\s*{_T})?\s*$", re.I)

# Lines that are structure, not sets.
_SKIP = re.compile(
    r"^\s*(?:#|//|$)|^\s*(?:stage|day|weekend|friday|saturday|sunday|monday|"
    r"tuesday|wednesday|thursday|set times?|schedule|lineup)\s*[:\-–—]?\s*$", re.I)

_DEFAULT_MIN = 50           # assumed set length when only a start time is given


def _to_minutes(h: str, m: str | None, ap: str | None) -> int | None:
    try:
        hh = int(h)
    except (TypeError, ValueError):
        return None
    mm = int(m) if m else 0
    if ap:
        ap = ap.replace(".", "").lower()
        if hh == 12:
            hh = 0
        if ap.startswith("p"):
            hh += 12
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh * 60 + mm


def parse_lines(text: str) -> tuple[list[dict], list[str]]:
    """Return (entries, unparsed). Entries carry minutes-since-midnight only."""
    entries, bad = [], []
    for raw in (text or "").splitlines():
        line = raw.strip().rstrip(",;")
        if not line or _SKIP.match(line):
            continue
        got = None
        m = _LEADING.match(line)
        if m:
            g = m.groups()
            start = _to_minutes(g[0], g[1], g[2])
            end = _to_minutes(g[3], g[4], g[5]) if g[3] else None
            got = (start, end, g[6])
        if got is None:
            m = _TRAILING.match(line)
            if m:
                g = m.groups()
                start = _to_minutes(g[1], g[2], g[3])
                end = _to_minutes(g[4], g[5], g[6]) if g[4] else None
                got = (start, end, g[0])
        if got is None or got[0] is None or not (got[2] or "").strip():
            bad.append(raw.strip())
            continue
        artist = re.sub(r"\s{2,}", " ", got[2]).strip(" -–—·:|")
        if not artist or len(artist) > 120:
            bad.append(raw.strip())
            continue
        entries.append({"artist": artist, "start_min": got[0], "end_min": got[1],
                        "raw": line})
    return entries, bad


def build_segments(text: str, day: str, tz_name: str,
                   default_minutes: int = _DEFAULT_MIN) -> tuple[list[dict], list[str]]:
    """
    Turn pasted text into absolute epoch times.

    `day` is the festival day the schedule belongs to (YYYY-MM-DD, local).
    Entries whose clock time goes BACKWARDS are treated as the following day —
    that is how a 20:00 / 21:30 / 00:40 running order is meant to read.
    """
    entries, bad = parse_lines(text)
    if not entries:
        return [], bad
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        tz = timezone.utc
        bad.append(f"(unknown timezone {tz_name!r}; treated times as UTC)")
    try:
        base = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=tz)
    except ValueError:
        return [], bad + [f"(bad day {day!r}, expected YYYY-MM-DD)"]

    out, prev, rollover = [], None, 0
    for e in entries:
        if prev is not None and e["start_min"] < prev - 60:
            rollover += 1                     # crossed midnight
        prev = e["start_min"]
        start = base + timedelta(days=rollover, minutes=e["start_min"])
        if e["end_min"] is not None:
            end_off = e["end_min"] + (1440 if e["end_min"] < e["start_min"] else 0)
            end = base + timedelta(days=rollover, minutes=end_off)
        else:
            end = start + timedelta(minutes=default_minutes)
        out.append({
            "artist": e["artist"],
            "starts_at": int(start.timestamp()),
            "ends_at": int(end.timestamp()),
            "raw_time": e["raw"],
            "local_start": start.strftime("%Y-%m-%d %H:%M %Z"),
            "local_end": end.strftime("%H:%M"),
            "assumed_end": e["end_min"] is None,
        })
    return out, bad
