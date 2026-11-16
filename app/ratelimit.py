"""
ratelimit.py -- every external service's limits, declared in ONE place.

WHY THIS EXISTS
    Throttling used to live per-caller, beside the code making the request, with
    the limit stated in a comment. That arrangement produced three failures in
    quick succession, and the third is the most instructive:

      1. The throttle contradicted its own comment. A 6-second interval permits
         10 requests/minute — 14,400/day — while the comment beside it called
         that "a fraction of" one per minute. The arithmetic was inverted by a
         factor of ten and nobody proofreads arithmetic in a comment.

      2. An interval was mistaken for a budget. Six seconds sounds careful and
         still allows 14,400 calls a day. Spacing is not a ceiling.

      3. A limit was declared UNPUBLISHED after checking only the public docs.
         setlist.fm's public documentation states no figures, so "setlist.fm
         publishes no rate limit" looked verified — and was wrong. The limits
         are published on the API-key page, behind a login: max 2.0/second and
         max 1,440/day. An unauthenticated check cannot establish that a limit
         is unpublished, only that it is not on the page you read.

    Hence `source`, which is mandatory and must say WHERE a number came from,
    and `published`, which records what the vendor actually states as distinct
    from what we allow ourselves.

PUBLISHED vs OUR BUDGET
    Where a vendor publishes limits, our budget is set strictly BELOW them, so
    we trip our own ceiling first and never theirs. A self-imposed cap that can
    be exceeded by the vendor's is not a safety margin.

    Where nothing is published, that does not mean there is no limit — it means
    the limit is undocumented and enforced by blocking you. Those budgets are
    deliberately conservative: being slow costs a delayed setlist, while being
    blocked costs an hour or a day of every lookup, which is far worse for work
    that is never urgent.

TWO MECHANISMS, BOTH NEEDED
    min_interval  spacing between calls. Stops bursts.
    daily_cap     a rolling 24h ceiling. Stops a runaway.

    The cap is what makes the ceiling real, and it is PERSISTED, so restarting
    the container does not hand the app a fresh allowance.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

from . import config

_lock = threading.Lock()


class Limit:
    """One service's limits, and where each number came from.

    `source` is mandatory and is the whole point: it forces the distinction
    between a number a vendor published and a number we chose.
    """

    def __init__(self, service: str, min_interval: float, daily_cap: int,
                 source: str, published: str = "", notes: str = ""):
        self.service = service
        self.min_interval = min_interval      # our spacing
        self.daily_cap = daily_cap            # OUR budget, at or under published
        self.source = source
        self.published = published            # what the vendor actually states
        self.notes = notes

    def as_dict(self) -> dict:
        return {"service": self.service, "min_interval_s": self.min_interval,
                "daily_cap": self.daily_cap, "source": self.source,
                "published": self.published or "nothing published",
                "notes": self.notes}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


LIMITS: dict[str, Limit] = {
    # PUBLISHED: max 2.0 requests/second and max 1,440 requests/DAY.
    #
    # Note where that came from, because it cost a wrong conclusion once: these
    # figures are stated on the API-key page at setlist.fm/settings/api, which
    # requires being logged in. The PUBLIC docs at api.setlist.fm/docs/1.0/
    # state no numbers at all, so checking only the public page produced a
    # confident "setlist.fm publishes no rate limit" — which was wrong. An
    # unauthenticated check is not proof that a limit is unpublished.
    #
    # 1,440/day is exactly one per minute sustained. Our spacing sits well
    # inside the 2/s ceiling and the daily budget sits under theirs, so we hit
    # our own limit first and never theirs — a self-imposed cap that can be
    # exceeded by the vendor's is not a safety margin.
    "setlistfm": Limit(
        "setlistfm",
        min_interval=_env_float("SETLISTFM_MIN_INTERVAL", 1.0),
        daily_cap=_env_int("SETLISTFM_DAILY_CAP", 1200),
        source="published — setlist.fm/settings/api (login required); "
               "confirmed 2026-08-21",
        published="max 2.0 requests/second, max 1,440 requests/day",
        notes="Non-commercial use only; attribution required. Our budget of "
              "1,200/day sits under the published 1,440 so we trip our own "
              "limit first. A 429 backs off for an hour."),

    # MusicBrainz DOES publish one: 1 request/second average, and it enforces
    # it with HTTP 503. Confirmed by hitting it during research on 2026-08-21.
    # The app does not currently call MusicBrainz directly — artist-lookup.py
    # goes through Lidarr, which owns its own throttle — so this entry exists
    # so that anything added later inherits the right number instead of
    # rediscovering it the hard way.
    "musicbrainz": Limit(
        "musicbrainz",
        min_interval=_env_float("MUSICBRAINZ_MIN_INTERVAL", 1.2),
        daily_cap=_env_int("MUSICBRAINZ_DAILY_CAP", 5000),
        source="published — musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting",
        published="1 request/second average, enforced with HTTP 503",
        notes="Requires a descriptive User-Agent with contact info. NOT called "
              "directly today; reached via Lidarr, which throttles itself."),

    # YouTube publishes nothing for either the Atom feed or the InnerTube
    # endpoint yt-dlp uses. The feed sends Cache-Control: max-age=900, which is
    # the only hard number available and is what discovery.py actually keys on —
    # asking more often returns byte-identical content. Metadata calls are the
    # expensive side; a tripped limit clears in roughly an hour.
    "youtube": Limit(
        "youtube",
        min_interval=_env_float("YOUTUBE_MIN_INTERVAL", 2.0),
        daily_cap=_env_int("YOUTUBE_DAILY_CAP", 4000),
        source="self-imposed — no published limit found; the feed's "
               "Cache-Control max-age=900 is the only stated figure",
        published="none published for the Atom feed or the InnerTube endpoint",
        notes="discovery.py enforces freshness, ±15% jitter and 4h backoff on "
              "429 separately; this entry pins the numbers."),

    # SponsorBlock's API docs (wiki.sponsor.ajay.app/w/API_Docs, read
    # 2026-11-16) state a 429 only for SUBMISSIONS; nothing is published for
    # the GET lookups we make. These numbers are ours. Lookups use 4-char hash
    # prefixes, so one request covers every id sharing a prefix, and results
    # are cached 6h in sponsorblock.py.
    "sponsorblock": Limit(
        "sponsorblock",
        min_interval=_env_float("SPONSORBLOCK_MIN_INTERVAL", 0.25),
        daily_cap=_env_int("SPONSORBLOCK_DAILY_CAP", 3000),
        source="self-imposed — API docs publish no limit for GET skipSegments "
               "(429 is documented for submissions only); checked 2026-11-16",
        published="none published for lookups",
        notes="Data is CC BY-NC-SA 4.0: non-commercial, attribution shown in "
              "the UI. Used only by the opt-in sponsor checks."),
}

_last_call: dict[str, float] = {}


def init_db() -> None:
    with _lock, _connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS rate_calls (
            service TEXT NOT NULL,
            at      INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_rate ON rate_calls(service, at);
        """)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def used(service: str) -> int:
    """Calls made in the last rolling 24 hours.

    Rolling rather than per-calendar-day on purpose: a midnight reset invites a
    burst at exactly the moment nobody is watching, and a rolling window makes
    the budget mean the same thing at every hour.
    """
    cutoff = int(time.time()) - 86400
    with _lock, _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM rate_calls WHERE service=? AND at>?",
            (service, cutoff)).fetchone()[0]


def remaining(service: str) -> int:
    lim = LIMITS.get(service)
    if not lim:
        return 1 << 30
    return max(0, lim.daily_cap - used(service))


def status(service: str) -> dict:
    lim = LIMITS.get(service)
    if not lim:
        return {"service": service, "known": False}
    u = used(service)
    return {**lim.as_dict(), "known": True, "used_24h": u,
            "remaining": max(0, lim.daily_cap - u),
            "exhausted": u >= lim.daily_cap}


def all_status() -> list[dict]:
    return [status(s) for s in LIMITS]


def _record(service: str) -> None:
    now = int(time.time())
    with _lock, _connect() as conn:
        conn.execute("INSERT INTO rate_calls (service, at) VALUES (?,?)",
                     (service, now))
        # Prune opportunistically. The window is 24h, so anything older is dead
        # weight; keeping a margin makes the count cheap without a cron job.
        conn.execute("DELETE FROM rate_calls WHERE at < ?", (now - 172800,))


def acquire(service: str) -> tuple[bool, str]:
    """Wait out the interval and claim one unit of the daily budget.

    Returns (ok, reason). A False is NOT an error — it means the budget is spent
    and the caller should skip cleanly and try later. Callers must treat it that
    way; turning an exhausted budget into a failure would make a deliberately
    conservative limit look like a broken integration.
    """
    lim = LIMITS.get(service)
    if not lim:
        return True, ""
    if used(service) >= lim.daily_cap:
        return False, (f"{service}: the self-imposed daily budget of "
                       f"{lim.daily_cap} requests is spent; it refills as the "
                       "rolling 24h window moves")
    with _lock:
        wait = lim.min_interval - (time.time() - _last_call.get(service, 0.0))
    if wait > 0:
        time.sleep(wait)
    with _lock:
        _last_call[service] = time.time()
    _record(service)
    return True, ""
