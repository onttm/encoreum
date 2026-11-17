"""
catalog.py -- the shared concert queue: sources, discovered events, requests.

This is the data layer for "Seerr, for live concerts": the family adds channels
(sources), a poller discovers upcoming broadcasts on them (events), and people
ask for the ones they want recorded (requests). An approved request becomes an
ordinary Encoreum job — there is no second pipeline.

DESIGN NOTES

  * Lives in the SAME SQLite file as jobs (/config/jobs.db) so a request and the
    job it produced can be joined, and one backup covers everything.

  * `etag` / `last_modified` are stored ON THE SOURCE ROW so every poll is a
    conditional HTTP request. YouTube publishes no rate limit for the feed
    endpoint, so behaving like a well-mannered HTTP client IS the protection:
    an unchanged feed costs ~300 bytes and zero yt-dlp calls.

  * Events carry `thumbnail_url` and `description` because the public Atom feed
    already contains both (verified against a real entry). The entire browse UI
    can therefore render with NO yt-dlp requests; yt-dlp is only asked the one
    question a feed cannot answer — "is this a scheduled stream, and when?".

  * `ephemeral` on a source encodes the product thesis: a broadcast is worth
    capturing in inverse proportion to how likely it is to be rehosted. An ACL
    taping is gone when it is gone; a festival main stream will be superseded by
    cleaner per-artist uploads within weeks. Auto-record should only ever
    default on for the ephemeral tier.
"""
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from . import config

_db_lock = threading.Lock()

# event.live_status values worth showing on a "what's coming" page
ACTIONABLE = ("is_upcoming", "is_live")

# TWO INDEPENDENT STATUS AXES — the structural idea taken from Seerr, whose
# running instance separates media_request.status (the human workflow) from
# media.status (system availability). A request can be APPROVED while its media
# is still PROCESSING; collapsing them into one field loses that, and the UI
# needs both to render honestly.
#
#   requests.status       what a PERSON decided
#   events.library_status where the FILE actually is
REQ_PENDING, REQ_APPROVED, REQ_DECLINED, REQ_DONE = (
    "pending", "approved", "declined", "done")
LIB_NONE, LIB_RECORDING, LIB_PARTIAL, LIB_AVAILABLE, LIB_FAILED = (
    "none", "recording", "partial", "available", "failed")

# Sentinel for set_user_pref(): "clear this override and go back to inheriting
# the Authentik tier". Needed because plain None already means "caller did not
# mention this field, leave it alone" — conflating the two made an override
# impossible to remove once set.
INHERIT = "__inherit__"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create the catalog tables. Safe to call repeatedly; runs at startup."""
    with _db_lock, _connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sources (
            id            TEXT PRIMARY KEY,
            platform      TEXT NOT NULL DEFAULT 'youtube',
            channel_id    TEXT NOT NULL,
            title         TEXT NOT NULL,
            show          TEXT NOT NULL,      -- Plex series to file captures under
            destination   TEXT NOT NULL DEFAULT 'concerts',
            ephemeral     INTEGER NOT NULL DEFAULT 1,
            auto_record   INTEGER NOT NULL DEFAULT 0,
            approved      INTEGER NOT NULL DEFAULT 1,
            added_by      TEXT,
            added_at      TEXT NOT NULL,
            last_polled   TEXT,
            poll_failures INTEGER NOT NULL DEFAULT 0,
            etag          TEXT,
            last_modified TEXT,
            fresh_until   INTEGER,             -- epoch; from Cache-Control max-age
            UNIQUE (platform, channel_id)
        );

        CREATE TABLE IF NOT EXISTS events (
            id             TEXT PRIMARY KEY,
            source_id      TEXT NOT NULL,
            platform_id    TEXT NOT NULL,       -- e.g. the YouTube video id
            url            TEXT NOT NULL,
            title          TEXT NOT NULL,
            description    TEXT,
            thumbnail_url  TEXT,
            published_at   TEXT,
            scheduled_at   INTEGER,             -- epoch seconds; NULL if unknown
            live_status    TEXT,
            classification TEXT,
            discovered_at  TEXT NOT NULL,
            checked_at     TEXT,
            job_id         TEXT,
            library_status TEXT NOT NULL DEFAULT 'none',
            UNIQUE (source_id, platform_id),
            FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS requests (
            id           TEXT PRIMARY KEY,
            event_id     TEXT NOT NULL,
            requested_by TEXT NOT NULL,          -- stable Authentik username
            requested_by_display TEXT,           -- human name; see note below
            status       TEXT NOT NULL DEFAULT 'pending',
            note         TEXT,
            created_at   TEXT NOT NULL,
            decided_by   TEXT,
            decided_at   TEXT,
            is_auto      INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
        );

        -- FESTIVALS -> STAGES -> SEGMENTS
        --
        -- Modelled in three levels because a festival is not one stream. Coachella
        -- runs SEVEN stages as seven separate YouTube streams (verified), each
        -- carrying many acts back to back. So:
        --
        --   festival   Coachella 2027
        --     stage    Sahara              <- one stream, once it exists
        --       segment  Four Tet 21:40-22:40   <- what someone actually wants
        --
        -- Stages are deliberately DECOUPLED from events. Set times are published
        -- days ahead, but the stream (the event) only appears when the broadcast
        -- is scheduled — so a stage carries event_id NULL until they are linked.
        -- Modelling a stage as an event directly would make schedule ingest
        -- impossible until the last minute.
        CREATE TABLE IF NOT EXISTS festivals (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            year        INTEGER,
            timezone    TEXT NOT NULL DEFAULT 'America/Los_Angeles',
            show        TEXT NOT NULL,        -- Plex series to file sets under
            destination TEXT NOT NULL DEFAULT 'concerts',
            notes       TEXT,
            created_at  TEXT NOT NULL,
            UNIQUE (name, year)
        );

        CREATE TABLE IF NOT EXISTS stages (
            id          TEXT PRIMARY KEY,
            festival_id TEXT NOT NULL,
            name        TEXT NOT NULL,
            event_id    TEXT,                 -- the stream, once discovered
            stream_url  TEXT,                 -- or a known URL before discovery
            created_at  TEXT NOT NULL,
            UNIQUE (festival_id, name),
            FOREIGN KEY (festival_id) REFERENCES festivals(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS segments (
            id            TEXT PRIMARY KEY,
            stage_id      TEXT NOT NULL,
            artist        TEXT NOT NULL,
            artist_mbid   TEXT,
            genres        TEXT,               -- JSON list, resolved at ingest
            library_note  TEXT,               -- "My Plex has 5 albums, no live footage"
            starts_at     INTEGER NOT NULL,   -- epoch seconds, UTC
            ends_at       INTEGER,
            raw_time      TEXT,               -- what the schedule literally said
            schedule_src  TEXT,               -- where the times came from
            created_at    TEXT NOT NULL,
            UNIQUE (stage_id, artist, starts_at),
            FOREIGN KEY (stage_id) REFERENCES stages(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_seg_start   ON segments(starts_at);
        CREATE INDEX IF NOT EXISTS idx_seg_stage   ON segments(stage_id);
        CREATE INDEX IF NOT EXISTS idx_stage_fest  ON stages(festival_id);
        CREATE INDEX IF NOT EXISTS idx_src_fresh    ON sources(fresh_until);
        -- Per-user overrides set from INSIDE the app, so auto-approve does not
        -- require editing Authentik groups. NULL/absent = fall back to the tier
        -- the user's Authentik groups grant.
        CREATE TABLE IF NOT EXISTS user_prefs (
            username     TEXT PRIMARY KEY,
            display      TEXT,
            auto_approve INTEGER,                -- 1 on, 0 off, NULL = inherit
            blocked      INTEGER NOT NULL DEFAULT 0,
            quota_limit  INTEGER,                -- requests per quota_days
            quota_days   INTEGER,
            note         TEXT,
            updated_by   TEXT,
            updated_at   TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_events_sched  ON events(scheduled_at);
        CREATE INDEX IF NOT EXISTS idx_events_status ON events(live_status);
        CREATE INDEX IF NOT EXISTS idx_events_source ON events(source_id);
        CREATE INDEX IF NOT EXISTS idx_req_status    ON requests(status);
        """)
        # Additive migration: SQLite has no ADD COLUMN IF NOT EXISTS.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sources)")}
        if "fresh_until" not in cols:
            conn.execute("ALTER TABLE sources ADD COLUMN fresh_until INTEGER")
        ecols = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
        if "library_status" not in ecols:
            conn.execute("ALTER TABLE events ADD COLUMN library_status TEXT "
                         "NOT NULL DEFAULT 'none'")
        rcols = {r["name"] for r in conn.execute("PRAGMA table_info(requests)")}
        if "is_auto" not in rcols:
            conn.execute("ALTER TABLE requests ADD COLUMN is_auto INTEGER "
                         "NOT NULL DEFAULT 0")
        if "requested_by_display" not in rcols:
            conn.execute("ALTER TABLE requests ADD COLUMN requested_by_display TEXT")
        if "segment_id" not in rcols:
            # A request targets EITHER a whole event (an ephemeral broadcast) or
            # a single segment (one set at a festival). event_id stays populated
            # in both cases so existing joins keep working.
            conn.execute("ALTER TABLE requests ADD COLUMN segment_id TEXT")


# --- sources ----------------------------------------------------------------
def add_source(platform: str, channel_id: str, title: str, show: str,
               destination: str = "concerts", ephemeral: bool = True,
               auto_record: bool = False, added_by: str = "") -> str:
    sid = uuid.uuid4().hex[:12]
    with _db_lock, _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO sources (id, platform, channel_id, title, show, "
                "destination, ephemeral, auto_record, added_by, added_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sid, platform, channel_id, title, show, destination,
                 int(ephemeral), int(auto_record), added_by, _now()))
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT id FROM sources WHERE platform=? AND channel_id=?",
                (platform, channel_id)).fetchone()
            return row["id"] if row else ""
    return sid


def list_sources(approved_only: bool = False) -> list[dict]:
    q = "SELECT * FROM sources"
    if approved_only:
        q += " WHERE approved=1"
    q += " ORDER BY title COLLATE NOCASE"
    with _db_lock, _connect() as conn:
        return [dict(r) for r in conn.execute(q)]


def get_source(source_id: str) -> dict | None:
    with _db_lock, _connect() as conn:
        r = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    return dict(r) if r else None


def set_source_approved(source_id: str, approved: bool) -> None:
    """Unapproved sources are NOT polled (list_sources(approved_only=True))."""
    with _db_lock, _connect() as conn:
        conn.execute("UPDATE sources SET approved=? WHERE id=?",
                     (int(bool(approved)), source_id))


def delete_source(source_id: str) -> None:
    with _db_lock, _connect() as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("DELETE FROM events WHERE source_id=?", (source_id,))
        conn.execute("DELETE FROM sources WHERE id=?", (source_id,))


def record_poll(source_id: str, etag: str | None, last_modified: str | None,
                failed: bool = False, fresh_until: int | None = None) -> None:
    """Record a poll. `fresh_until` comes from the response's Cache-Control.

    MEASURED 2026-08-21: YouTube's feed endpoint returns NO ETag and NO
    Last-Modified, so conditional requests can never yield a 304 — the
    validators are kept only in case that changes. What it DOES return is
    `Cache-Control: public, max-age=900`, so the real saving is not re-fetching
    at all while the CDN copy is still fresh. That also puts a principled floor
    under the poll interval: asking more often than every 15 minutes returns
    byte-identical content.
    """
    with _db_lock, _connect() as conn:
        if failed:
            conn.execute(
                "UPDATE sources SET last_polled=?, poll_failures=poll_failures+1 "
                "WHERE id=?", (_now(), source_id))
        else:
            conn.execute(
                "UPDATE sources SET last_polled=?, poll_failures=0, etag=?, "
                "last_modified=?, fresh_until=? WHERE id=?",
                (_now(), etag, last_modified, fresh_until, source_id))


# --- events -----------------------------------------------------------------
def upsert_event(source_id: str, platform_id: str, url: str, title: str,
                 description: str = "", thumbnail_url: str = "",
                 published_at: str = "") -> str:
    """Insert a newly-seen broadcast, or refresh the feed-derived fields.

    Deliberately does NOT touch live_status / scheduled_at — those come from
    yt-dlp via update_event_status() and must not be clobbered by a feed refresh.
    """
    with _db_lock, _connect() as conn:
        row = conn.execute(
            "SELECT id FROM events WHERE source_id=? AND platform_id=?",
            (source_id, platform_id)).fetchone()
        if row:
            conn.execute(
                "UPDATE events SET title=?, description=?, thumbnail_url=? WHERE id=?",
                (title, description, thumbnail_url, row["id"]))
            return row["id"]
        eid = uuid.uuid4().hex[:12]
        conn.execute(
            "INSERT INTO events (id, source_id, platform_id, url, title, description, "
            "thumbnail_url, published_at, discovered_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (eid, source_id, platform_id, url, title, description,
             thumbnail_url, published_at, _now()))
        return eid


def get_event_id(source_id: str, platform_id: str) -> str | None:
    """Used by the poller to tell a genuinely new entry from a refresh."""
    with _db_lock, _connect() as conn:
        r = conn.execute(
            "SELECT id FROM events WHERE source_id=? AND platform_id=?",
            (source_id, platform_id)).fetchone()
    return r["id"] if r else None


def update_event_status(event_id: str, live_status: str,
                        scheduled_at: int | None,
                        classification: str = "") -> None:
    with _db_lock, _connect() as conn:
        conn.execute(
            "UPDATE events SET live_status=?, scheduled_at=?, checked_at=?"
            + (", classification=?" if classification else "")
            + " WHERE id=?",
            ((live_status, scheduled_at, _now(), classification, event_id)
             if classification else (live_status, scheduled_at, _now(), event_id)))


def set_library_status(event_id: str, status: str) -> None:
    """Where the FILE is — independent of what anyone decided about it."""
    with _db_lock, _connect() as conn:
        conn.execute("UPDATE events SET library_status=? WHERE id=?",
                     (status, event_id))


def request_counts() -> dict:
    """One cheap aggregate for a nav badge, modelled on Seerr's /request/count.

    Cheap enough to poll from the header, which is why Seerr exposes it as its
    own endpoint rather than making the client count a paginated list.
    """
    with _db_lock, _connect() as conn:
        rows = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) n FROM requests GROUP BY status")}
        lib = {r["library_status"]: r["n"] for r in conn.execute(
            "SELECT library_status, COUNT(*) n FROM events GROUP BY library_status")}
        auto = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE is_auto=1").fetchone()[0]
    return {
        "total": sum(rows.values()),
        "pending": rows.get(REQ_PENDING, 0),
        "approved": rows.get(REQ_APPROVED, 0),
        "declined": rows.get(REQ_DECLINED, 0),
        "completed": rows.get(REQ_DONE, 0),
        "auto": auto,
        "recording": lib.get(LIB_RECORDING, 0),
        "available": lib.get(LIB_AVAILABLE, 0),
    }


def attach_job(event_id: str, job_id: str) -> None:
    with _db_lock, _connect() as conn:
        conn.execute("UPDATE events SET job_id=? WHERE id=?", (job_id, event_id))


def events_needing_check(limit: int = 40) -> list[dict]:
    """Entries whose live_status is unknown or still pending.

    Settled entries (was_live / not_live) are skipped — they cannot become live
    again, so re-checking them would spend yt-dlp requests for nothing. Anything
    still `is_upcoming` IS re-checked every pass, because its status flips to
    is_live at showtime WITHOUT the feed changing.
    """
    with _db_lock, _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT e.*, s.show, s.destination FROM events e "
            "JOIN sources s ON s.id = e.source_id "
            "WHERE e.live_status IS NULL OR e.live_status IN ('is_upcoming','post_live','') "
            "ORDER BY (e.live_status IS NULL) DESC, e.scheduled_at IS NULL, e.scheduled_at "
            "LIMIT ?", (limit,))]


def upcoming(limit: int = 100) -> list[dict]:
    """What the browse page shows: live now first, then soonest scheduled."""
    with _db_lock, _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT e.*, s.title AS source_title, s.show, s.ephemeral "
            "FROM events e JOIN sources s ON s.id = e.source_id "
            "WHERE e.live_status IN ('is_live','is_upcoming') "
            "ORDER BY (e.live_status='is_live') DESC, "
            "         e.scheduled_at IS NULL, e.scheduled_at ASC LIMIT ?", (limit,))]


def get_event(event_id: str) -> dict | None:
    with _db_lock, _connect() as conn:
        r = conn.execute(
            "SELECT e.*, s.show, s.destination, s.title AS source_title "
            "FROM events e JOIN sources s ON s.id = e.source_id WHERE e.id=?",
            (event_id,)).fetchone()
    return dict(r) if r else None


def prune_events(keep_days: int = 120) -> int:
    """Drop settled events older than keep_days that never produced a job."""
    cutoff = datetime.now(timezone.utc).timestamp() - keep_days * 86400
    iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat(timespec="seconds")
    with _db_lock, _connect() as conn:
        cur = conn.execute(
            "DELETE FROM events WHERE job_id IS NULL AND discovered_at < ? "
            "AND live_status IN ('was_live','not_live')", (iso,))
        return cur.rowcount


# --- requests ---------------------------------------------------------------
def add_request(event_id: str, requested_by: str, note: str = "",
                status: str = REQ_PENDING, is_auto: bool = False,
                display: str = "") -> str:
    """`is_auto` separates machine-generated requests from human ones.

    Worth the column: on the household Seerr instance 171 of 288 requests were
    auto-generated, so without the flag a request history is mostly noise.

    `display` is stored ALONGSIDE the username, not instead of it. The username
    is the stable key, but 5 of the 18 Plex-linked accounts have opaque
    usernames (plex-109535685 and friends) — a request list keyed on username
    alone would show numeric strings nobody can identify. Their display names
    are Colby, Donna, Koyi, Guest and Jason. Storing it at request time also
    survives someone later renaming themselves in Plex.
    """
    rid = uuid.uuid4().hex[:12]
    with _db_lock, _connect() as conn:
        conn.execute(
            "INSERT INTO requests (id, event_id, requested_by, requested_by_display, "
            "status, note, created_at, is_auto) VALUES (?,?,?,?,?,?,?,?)",
            (rid, event_id, requested_by, display or requested_by, status, note,
             _now(), int(is_auto)))
    return rid


def list_requests(status: str = "") -> list[dict]:
    q = ("SELECT r.*, e.title AS event_title, e.url, e.scheduled_at, e.thumbnail_url "
         "FROM requests r JOIN events e ON e.id = r.event_id")
    args: tuple = ()
    if status:
        q += " WHERE r.status=?"
        args = (status,)
    q += " ORDER BY r.created_at DESC"
    with _db_lock, _connect() as conn:
        return [dict(r) for r in conn.execute(q, args)]


def request_feed(limit: int = 60) -> list[dict]:
    """The shared activity feed — what everyone sees, Seerr-style.

    Deliberately visible to every user who can browse, not just to admins.
    Seeing that someone already asked for a show is what stops three people
    requesting the same concert, and it makes a shared library feel shared.
    Note it exposes WHO asked for WHAT to the whole household by design.
    """
    # LEFT JOINs are load-bearing. A request for a festival SET has no event
    # until that stage's stream is discovered — the schedule is published days
    # ahead of the broadcast. Inner-joining events silently dropped every set
    # request from the feed, which is the worst kind of bug: the request existed,
    # was counted, and was invisible.
    with _db_lock, _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT r.id, r.status, r.created_at, r.decided_at, r.is_auto, r.note, "
            "       r.requested_by, r.segment_id, "
            "       COALESCE(NULLIF(r.requested_by_display,''), r.requested_by) AS who, "
            "       r.decided_by, "
            "       e.title AS event_title, e.url, e.thumbnail_url, e.scheduled_at, "
            "       e.live_status, e.library_status, "
            "       src.title AS source_title, "
            "       g.artist AS set_artist, g.starts_at AS set_starts_at, "
            "       g.genres AS set_genres, g.library_note AS set_library_note, "
            "       st.name AS stage, f.name AS festival, "
            "       COALESCE(e.title, g.artist) AS title, "
            "       CASE WHEN r.segment_id IS NOT NULL THEN 'set' ELSE 'broadcast' END AS kind "
            "FROM requests r "
            "LEFT JOIN events e   ON e.id = r.event_id "
            "LEFT JOIN sources src ON src.id = e.source_id "
            "LEFT JOIN segments g ON g.id = r.segment_id "
            "LEFT JOIN stages st  ON st.id = g.stage_id "
            "LEFT JOIN festivals f ON f.id = st.festival_id "
            "ORDER BY r.created_at DESC LIMIT ?", (limit,))]


def get_user_pref(username: str) -> dict | None:
    with _db_lock, _connect() as conn:
        r = conn.execute("SELECT * FROM user_prefs WHERE username=?",
                         (username,)).fetchone()
    return dict(r) if r else None


def list_user_prefs() -> list[dict]:
    with _db_lock, _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM user_prefs ORDER BY username COLLATE NOCASE")]


def set_user_pref(username: str, *, display: str = "", auto_approve=None,
                  blocked: bool | None = None, quota_limit=None,
                  quota_days=None, note: str | None = None,
                  updated_by: str = "") -> None:
    """Upsert a per-user override. auto_approve=None means 'inherit the tier'."""
    cur = get_user_pref(username) or {}
    if auto_approve == INHERIT:
        aa = None                                   # clear -> inherit the tier
    elif auto_approve is None:
        aa = cur.get("auto_approve")                # unspecified -> unchanged
    else:
        aa = int(bool(auto_approve))
    row = {
        "display": display or cur.get("display") or username,
        "auto_approve": aa,
        "blocked": cur.get("blocked", 0) if blocked is None else int(bool(blocked)),
        "quota_limit": cur.get("quota_limit") if quota_limit is None else quota_limit,
        "quota_days": cur.get("quota_days") if quota_days is None else quota_days,
        "note": cur.get("note") if note is None else note,
    }
    with _db_lock, _connect() as conn:
        conn.execute(
            "INSERT INTO user_prefs (username, display, auto_approve, blocked, "
            "quota_limit, quota_days, note, updated_by, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(username) DO UPDATE SET display=excluded.display, "
            "auto_approve=excluded.auto_approve, blocked=excluded.blocked, "
            "quota_limit=excluded.quota_limit, quota_days=excluded.quota_days, "
            "note=excluded.note, updated_by=excluded.updated_by, "
            "updated_at=excluded.updated_at",
            (username, row["display"], row["auto_approve"], row["blocked"],
             row["quota_limit"], row["quota_days"], row["note"], updated_by, _now()))


def requests_in_window(username: str, days: int) -> int:
    """How many requests this user made in the last `days` — for quota checks."""
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat(timespec="seconds")
    with _db_lock, _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM requests WHERE requested_by=? AND created_at>=? "
            "AND is_auto=0", (username, iso)).fetchone()[0]


def set_request_status(request_id: str, status: str, decided_by: str = "") -> None:
    with _db_lock, _connect() as conn:
        conn.execute(
            "UPDATE requests SET status=?, decided_by=?, decided_at=? WHERE id=?",
            (status, decided_by, _now(), request_id))


def stats() -> dict:
    with _db_lock, _connect() as conn:
        def one(sql, *a):
            return conn.execute(sql, a).fetchone()[0]
        return {
            "sources": one("SELECT COUNT(*) FROM sources"),
            "events": one("SELECT COUNT(*) FROM events"),
            "upcoming": one("SELECT COUNT(*) FROM events WHERE live_status='is_upcoming'"),
            "live": one("SELECT COUNT(*) FROM events WHERE live_status='is_live'"),
            "requests_pending": one("SELECT COUNT(*) FROM requests WHERE status='pending'"),
        }


# --- festivals / stages / segments -------------------------------------------
def add_festival(name: str, year: int, show: str = "", timezone_name: str = "",
                 destination: str = "concerts", notes: str = "") -> str:
    fid = uuid.uuid4().hex[:12]
    with _db_lock, _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO festivals (id, name, year, timezone, show, destination, "
                "notes, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (fid, name, year, timezone_name or "America/Los_Angeles",
                 show or f"{name} {year}", destination, notes, _now()))
        except sqlite3.IntegrityError:
            r = conn.execute("SELECT id FROM festivals WHERE name=? AND year=?",
                             (name, year)).fetchone()
            return r["id"] if r else ""
    return fid


def list_festivals() -> list[dict]:
    with _db_lock, _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT f.*, "
            "  (SELECT COUNT(*) FROM stages s WHERE s.festival_id=f.id) AS stage_count, "
            "  (SELECT COUNT(*) FROM segments g JOIN stages s ON s.id=g.stage_id "
            "     WHERE s.festival_id=f.id) AS set_count "
            "FROM festivals f ORDER BY f.year DESC, f.name")]


def get_festival(fid: str) -> dict | None:
    with _db_lock, _connect() as conn:
        r = conn.execute("SELECT * FROM festivals WHERE id=?", (fid,)).fetchone()
    return dict(r) if r else None


def add_stage(festival_id: str, name: str, stream_url: str = "",
              event_id: str = "") -> str:
    sid = uuid.uuid4().hex[:12]
    with _db_lock, _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO stages (id, festival_id, name, event_id, stream_url, "
                "created_at) VALUES (?,?,?,?,?,?)",
                (sid, festival_id, name, event_id or None, stream_url or None, _now()))
        except sqlite3.IntegrityError:
            r = conn.execute("SELECT id FROM stages WHERE festival_id=? AND name=?",
                             (festival_id, name)).fetchone()
            return r["id"] if r else ""
    return sid


def link_stage_stream(stage_id: str, event_id: str = "", stream_url: str = "") -> None:
    """Attach the actual stream to a stage once it is known."""
    with _db_lock, _connect() as conn:
        conn.execute("UPDATE stages SET event_id=COALESCE(?,event_id), "
                     "stream_url=COALESCE(?,stream_url) WHERE id=?",
                     (event_id or None, stream_url or None, stage_id))


def list_stages(festival_id: str) -> list[dict]:
    with _db_lock, _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM segments g WHERE g.stage_id=s.id) "
            "AS set_count FROM stages s WHERE s.festival_id=? ORDER BY s.name",
            (festival_id,))]


def add_segment(stage_id: str, artist: str, starts_at: int, ends_at: int | None = None,
                raw_time: str = "", schedule_src: str = "", artist_mbid: str = "",
                genres: list | None = None, library_note: str = "") -> str:
    gid = uuid.uuid4().hex[:12]
    with _db_lock, _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO segments (id, stage_id, artist, artist_mbid, genres, "
                "library_note, starts_at, ends_at, raw_time, schedule_src, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (gid, stage_id, artist, artist_mbid or None,
                 json.dumps(genres) if genres else None, library_note or None,
                 starts_at, ends_at, raw_time, schedule_src, _now()))
        except sqlite3.IntegrityError:
            r = conn.execute(
                "SELECT id FROM segments WHERE stage_id=? AND artist=? AND starts_at=?",
                (stage_id, artist, starts_at)).fetchone()
            return r["id"] if r else ""
    return gid


def enrich_segment(segment_id: str, artist_mbid: str = "", genres: list | None = None,
                   library_note: str = "") -> None:
    with _db_lock, _connect() as conn:
        conn.execute(
            "UPDATE segments SET artist_mbid=COALESCE(?,artist_mbid), "
            "genres=COALESCE(?,genres), library_note=COALESCE(?,library_note) "
            "WHERE id=?",
            (artist_mbid or None, json.dumps(genres) if genres else None,
             library_note or None, segment_id))


def segments_unenriched(limit: int = 30) -> list[dict]:
    with _db_lock, _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM segments WHERE artist_mbid IS NULL "
            "ORDER BY starts_at LIMIT ?", (limit,))]


def festival_schedule(festival_id: str = "", upcoming_only: bool = False,
                      limit: int = 500) -> list[dict]:
    """The set list — what a person picks from. Ordered by start time."""
    q = ("SELECT g.*, s.name AS stage, s.event_id, s.stream_url, "
         "       f.id AS festival_id, f.name AS festival, f.year, f.timezone, "
         "       f.show, f.destination, "
         "       (SELECT COUNT(*) FROM requests r WHERE r.segment_id = g.id) AS requests "
         "FROM segments g "
         "JOIN stages s ON s.id = g.stage_id "
         "JOIN festivals f ON f.id = s.festival_id")
    args: list = []
    where = []
    if festival_id:
        where.append("f.id = ?")
        args.append(festival_id)
    if upcoming_only:
        where.append("g.starts_at > ?")
        args.append(int(datetime.now(timezone.utc).timestamp()))
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY g.starts_at, s.name LIMIT ?"
    args.append(limit)
    with _db_lock, _connect() as conn:
        rows = [dict(r) for r in conn.execute(q, args)]
    for r in rows:
        try:
            r["genres"] = json.loads(r["genres"]) if r["genres"] else []
        except (TypeError, ValueError):
            r["genres"] = []
    return rows


def get_segment(segment_id: str) -> dict | None:
    with _db_lock, _connect() as conn:
        r = conn.execute(
            "SELECT g.*, s.name AS stage, s.event_id, s.stream_url, "
            "       f.name AS festival, f.year, f.timezone, f.show, f.destination "
            "FROM segments g JOIN stages s ON s.id=g.stage_id "
            "JOIN festivals f ON f.id=s.festival_id WHERE g.id=?",
            (segment_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    try:
        d["genres"] = json.loads(d["genres"]) if d["genres"] else []
    except (TypeError, ValueError):
        d["genres"] = []
    return d
