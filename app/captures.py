"""
captures.py -- metadata for a recorded set, written in two stages.

THE PROBLEM
    A festival set is recorded while it airs. At that moment nobody has posted
    its setlist — setlists are written up by people who were there, hours to
    days later, and a large share of festival sets never get one at all.

    So metadata cannot be a single event at capture time. It arrives twice:

        stage 1, at capture     artist, festival, stage, performance date,
                                set times, stream, library note
        stage 2, later (maybe)  song-by-song setlist, tour name, confirmed
                                venue and city

STAGE 1 IS A COMPLETE STATE, NOT A BROKEN ONE
    `minimal` is a finished, valid outcome. The file is named correctly, filed
    in the right season, and plays. Everything in stage 1 comes from what the
    app itself knew when it pressed record — it chose the artist, the stage and
    the stream — so none of it is a guess waiting to be confirmed.

    This matters for how it is presented: a capture sitting at `minimal` is not
    an error, a warning, or a job half-done, and must never be rendered as one.
    Most of them will stay that way forever and that is the expected outcome.

STAGE 2 IS AN UPGRADE THAT MAY NEVER COME
    Enrichment retries on a widening schedule and then STOPS, settling at
    `unavailable` — a normal terminal state meaning "nobody wrote this one up",
    not a failure. Retrying forever would burn a rate limit that is shared with
    every other capture, for the least likely results.

    The schedule widens because the probability of a setlist appearing is highly
    front-loaded: most that will ever exist show up within about a day, a few
    trickle in over the following fortnight, and after that essentially none.

WHAT ENRICHMENT MAY NEVER DO
    Overwrite a human. Corrections live in `overrides` and win over both stages,
    permanently — the same rule imports.py applies, for the same reason. If a
    person fixed the artist because the schedule had it wrong, a setlist lookup
    keyed on the corrected name must not then re-import the wrong one.

PERFORMANCE DATE, NOT AIR DATE
    `performance_date` is the LOCAL date at the festival, and it is what
    setlist.fm is keyed on. It is stored explicitly rather than derived from the
    capture timestamp, because the two differ routinely: a set that starts
    23:40 local is already the next day in UTC, and delayed or replayed streams
    can air a full day after the performance. Deriving it would produce a date
    that is wrong exactly often enough to be baffling.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from . import config, setlistfm

_db_lock = threading.Lock()

# Human workflow / system state, kept apart the way catalog.py keeps
# requests.status apart from library_status.
MINIMAL, ENRICHED, UNAVAILABLE, MANUAL, REVIEW = (
    "minimal", "enriched", "unavailable", "manual", "review")

# Retry schedule in hours, measured from the END of the performance. Nine
# attempts across a week, tightest on the first day and thinning after: 6h and
# 12h catch the same-night write-ups, the 18/24/36/48 band catches the next-day
# ones, and 72/96/168 sweep the stragglers.
#
# After the last entry the capture settles at `unavailable` and is NEVER polled
# again. That is a deliberate end, not a failure: past a week a setlist almost
# certainly does not exist, and continuing to ask spends a shared budget on the
# least likely results. From then on it is manual — paste one in whenever you
# like, or hit /enrich to ask once on demand.
RETRY_HOURS = (6, 12, 18, 24, 36, 48, 72, 96, 168)      # 6h out to 7 days


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _db_lock, _connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS captures (
            id            TEXT PRIMARY KEY,
            segment_id    TEXT,               -- festival set, when it was one
            event_id      TEXT,
            job_id        TEXT,

            -- stage 1: what the app knew when it pressed record
            artist        TEXT NOT NULL,
            artist_mbid   TEXT,
            festival      TEXT,
            stage         TEXT,
            show          TEXT,
            performance_date TEXT,            -- LOCAL date at the festival, ISO
            starts_at     INTEGER,
            ends_at       INTEGER,
            venue         TEXT,
            city          TEXT,
            stream_url    TEXT,
            file_path     TEXT,
            track_count   INTEGER,           -- what a setlist has to cover

            -- stage 2: setlist.fm, if it ever turns up
            state         TEXT NOT NULL DEFAULT 'minimal',
            setlist       TEXT,               -- normalised JSON, APPLIED
            setlist_url   TEXT,               -- attribution, required by terms
            tour          TEXT,

            -- A found-but-not-certain setlist waits here instead of being
            -- applied. Kept apart from `setlist` so nothing downstream can
            -- mistake a proposal for a confirmed match.
            proposed_setlist TEXT,
            match_report  TEXT,               -- why it is or is not certain
            declined_ids  TEXT,               -- setlist ids a person rejected

            -- corrections; these beat both stages, permanently
            overrides     TEXT NOT NULL DEFAULT '{}',

            attempts      INTEGER NOT NULL DEFAULT 0,
            next_attempt_at INTEGER,
            last_reason   TEXT,
            created_at    TEXT NOT NULL,
            enriched_at   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_cap_state ON captures(state);
        CREATE INDEX IF NOT EXISTS idx_cap_next  ON captures(next_attempt_at);
        CREATE INDEX IF NOT EXISTS idx_cap_seg   ON captures(segment_id);
        """)
        _add_missing_columns(conn)


# Columns added after the table first shipped. CREATE TABLE IF NOT EXISTS does
# NOT alter an existing table, so a new column in the schema above is silently
# absent on any database that already exists — the code then reads it as None
# and the feature quietly does nothing. That is exactly how the setlist coverage
# check came to be skipped on a real capture: no error, no column, no check.
_LATER_COLUMNS = {
    "track_count": "INTEGER",
    "proposed_setlist": "TEXT",
    "match_report": "TEXT",
    "declined_ids": "TEXT",
}


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    have = {r[1] for r in conn.execute("PRAGMA table_info(captures)")}
    for col, decl in _LATER_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE captures ADD COLUMN {col} {decl}")


# --------------------------------------------------------------------------
# stage 1
# --------------------------------------------------------------------------

def record(artist: str, *, segment_id: str = "", event_id: str = "",
           job_id: str = "", festival: str = "", stage: str = "",
           show: str = "", performance_date: str = "", starts_at: int = 0,
           ends_at: int = 0, venue: str = "", city: str = "",
           stream_url: str = "", file_path: str = "", track_count: int = 0,
           artist_mbid: str = "") -> str:
    """Register a capture with the metadata available right now.

    Call this the moment a recording completes. It never blocks on setlist.fm
    and never fails because a setlist is missing — stage 1 stands on its own.
    """
    cid = uuid.uuid4().hex[:12]
    # Count the siblings when not told: a setlist claiming far fewer songs than
    # the recording has tracks is the signal that it covers only part of it.
    if not track_count and file_path:
        p = Path(file_path)
        if p.parent.is_dir():
            track_count = sum(1 for f in p.parent.iterdir()
                              if f.suffix.lower() in (".m4a", ".mp4", ".flac",
                                                      ".opus", ".mp3", ".mkv"))
    # First attempt is scheduled from the END of the set, not from now: a
    # capture that finishes early would otherwise ask before the show is over.
    base = ends_at or starts_at or int(time.time())
    with _db_lock, _connect() as conn:
        conn.execute(
            "INSERT INTO captures (id, segment_id, event_id, job_id, artist, "
            "artist_mbid, festival, stage, show, performance_date, starts_at, "
            "ends_at, venue, city, stream_url, file_path, track_count, state, "
            "next_attempt_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, segment_id or None, event_id or None, job_id or None,
             artist, artist_mbid or None, festival or None, stage or None,
             show or None, performance_date or None, starts_at or None,
             ends_at or None, venue or None, city or None, stream_url or None,
             file_path or None, track_count or None, MINIMAL,
             base + RETRY_HOURS[0] * 3600, _now()))
    return cid


def local_date(epoch: int, tz_name: str) -> str:
    """The festival-local calendar date of an epoch time.

    This is the value setlist.fm is keyed on. A 23:40 set is still the same
    festival day locally while already being tomorrow in UTC, so using a UTC
    date here would miss by one day for every late-night set.
    """
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz = timezone.utc
    return datetime.fromtimestamp(epoch, tz).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["overrides"] = json.loads(d.get("overrides") or "{}")
    d["setlist"] = json.loads(d["setlist"]) if d.get("setlist") else None
    d["proposed_setlist"] = (json.loads(d["proposed_setlist"])
                             if d.get("proposed_setlist") else None)
    d["match_report"] = (json.loads(d["match_report"])
                         if d.get("match_report") else None)
    d["declined_ids"] = json.loads(d.get("declined_ids") or "[]")
    eff = {}
    for k in ("artist", "festival", "stage", "venue", "city", "tour",
              "performance_date"):
        eff[k] = d["overrides"].get(k) or d.get(k)
    d["effective"] = eff
    d["corrected_fields"] = sorted(k for k, v in d["overrides"].items()
                                   if v not in (None, ""))
    d["has_setlist"] = bool(d["setlist"] and d["setlist"].get("tracks"))

    # THE MARK. One field, on every capture, answering "has this been matched to
    # a setlist?" so an un-matched download is visible at a glance rather than
    # something a person has to go looking for.
    d["setlist_match"] = {
        MINIMAL: "unmatched",
        REVIEW: "needs_review",
        ENRICHED: "matched",
        MANUAL: "matched_by_hand",
        UNAVAILABLE: "unmatched",
    }.get(d["state"], "unmatched")
    d["unmatched"] = d["setlist_match"] in ("unmatched", "needs_review")
    d["needs_review"] = d["state"] == REVIEW
    d["attribution"] = (setlistfm.ATTRIBUTION
                        if d.get("setlist") and (d["setlist"].get("source")
                                                 == "setlist.fm") else None)
    # Said plainly, because "minimal" must not read as an error anywhere.
    d["state_label"] = {
        MINIMAL: "basic metadata; waiting to see if a setlist is posted",
        ENRICHED: "setlist matched and applied automatically",
        UNAVAILABLE: ("no setlist was posted within a week of the show — "
                      "automatic polling has stopped; add one by hand any time"),
        MANUAL: "setlist entered by hand",
        REVIEW: "a setlist was found but is NOT a certain match — confirm it",
    }.get(d["state"], d["state"])
    return d


def get(cid: str) -> dict | None:
    with _db_lock, _connect() as conn:
        r = conn.execute("SELECT * FROM captures WHERE id=?", (cid,)).fetchone()
    return _row(r) if r else None


def listing(state: str = "", limit: int = 200) -> list[dict]:
    q, args = "SELECT * FROM captures", ()
    if state:
        q, args = q + " WHERE state=?", (state,)
    q += " ORDER BY COALESCE(starts_at, 0) DESC LIMIT ?"
    with _db_lock, _connect() as conn:
        return [_row(r) for r in conn.execute(q, (*args, limit))]


def stats() -> dict:
    with _db_lock, _connect() as conn:
        rows = dict(conn.execute(
            "SELECT state, COUNT(*) FROM captures GROUP BY state").fetchall())
        due = conn.execute(
            "SELECT COUNT(*) FROM captures WHERE state=? AND "
            "COALESCE(next_attempt_at, 0) <= ?",
            (MINIMAL, int(time.time()))).fetchone()[0]
    total = sum(rows.values())
    matched = rows.get(ENRICHED, 0) + rows.get(MANUAL, 0)
    return {"minimal": rows.get(MINIMAL, 0), "enriched": rows.get(ENRICHED, 0),
            "unavailable": rows.get(UNAVAILABLE, 0), "manual": rows.get(MANUAL, 0),
            "review": rows.get(REVIEW, 0),
            # The headline the UI should lead with: how many downloads carry no
            # confirmed setlist, and how many are waiting on a person.
            "total": total, "matched": matched, "unmatched": total - matched,
            "needs_review": rows.get(REVIEW, 0),
            "due_now": due, "lookup": setlistfm.available(),
            "attribution": setlistfm.ATTRIBUTION}


def _update(cid: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with _db_lock, _connect() as conn:
        conn.execute(f"UPDATE captures SET {cols} WHERE id=?",
                     (*fields.values(), cid))


# --------------------------------------------------------------------------
# stage 2
# --------------------------------------------------------------------------

def due(limit: int = 20) -> list[dict]:
    """Captures whose next lookup is owed. Only ever `minimal` ones.

    `manual` and `enriched` are excluded structurally: one has a human's
    setlist, the other already has one, and re-fetching either would replace
    known-good data with a lookup result.
    """
    with _db_lock, _connect() as conn:
        return [_row(r) for r in conn.execute(
            "SELECT * FROM captures WHERE state=? AND "
            "COALESCE(next_attempt_at, 0) <= ? ORDER BY next_attempt_at LIMIT ?",
            (MINIMAL, int(time.time()), limit))]


def _schedule_next(cap: dict) -> tuple[int | None, str]:
    n = cap["attempts"] + 1
    if n >= len(RETRY_HOURS):
        return None, UNAVAILABLE
    base = cap.get("ends_at") or cap.get("starts_at") or int(time.time())
    return base + RETRY_HOURS[n] * 3600, MINIMAL


def enrich_one(cid: str) -> dict:
    """Try once to attach a setlist. Never raises, never blocks a capture."""
    cap = get(cid)
    if not cap:
        return {"ok": False, "error": "no such capture"}
    if cap["state"] in (ENRICHED, MANUAL):
        return {"ok": True, "skipped": "already has a setlist"}

    eff = cap["effective"]
    d = eff.get("performance_date")
    if not d:
        _update(cid, state=UNAVAILABLE, next_attempt_at=None,
                last_reason="no performance date, so nothing to look up")
        return {"ok": False, "error": "no performance date"}

    # Prefer a corrected artist name, and drop the mbid if a human changed the
    # artist — an mbid resolved from the WRONG name would silently outrank the
    # correction, which is the exact failure the override exists to prevent.
    mbid = "" if "artist" in cap["corrected_fields"] else (cap.get("artist_mbid") or "")
    r = setlistfm.find(eff["artist"], d, mbid=mbid,
                       venue_hint=eff.get("venue") or eff.get("festival") or "")

    if not r.get("found"):
        nxt, state = _schedule_next(cap)
        _update(cid, attempts=cap["attempts"] + 1, next_attempt_at=nxt,
                state=state, last_reason=r.get("reason", "not found"))
        return {"ok": True, "found": False, "reason": r.get("reason"),
                "state": state,
                "next_attempt": (datetime.fromtimestamp(nxt, timezone.utc)
                                 .strftime("%Y-%m-%d %H:%M UTC") if nxt else None)}

    sl = r["setlist"]
    # Declines are recorded as "id@version". setlist.fm is a wiki, so a setlist
    # that was wrong when it was declined may since have been CORRECTED — and
    # keying the decline on the id alone would suppress the fixed version
    # forever. A changed versionId means it is genuinely a different setlist
    # now and deserves another look.
    stamp = f"{sl.get('setlist_id')}@{sl.get('version_id')}"
    if sl.get("setlist_id") and stamp in cap["declined_ids"]:
        nxt, state = _schedule_next(cap)
        _update(cid, attempts=cap["attempts"] + 1, next_attempt_at=nxt,
                state=state, last_reason="the only match found was already declined")
        return {"ok": True, "found": False, "reason": "already declined",
                "state": state}

    q = setlistfm.match_quality(sl, cap["effective"],
                                candidates=r.get("candidates", 1),
                                matched_by=r.get("matched_by", "name"),
                                track_count=cap.get("track_count") or 0)
    if q["exact"]:
        # Certain on every axis — apply it without asking.
        _apply_setlist(cid, cap, sl, ENRICHED, match=q)
        return {"ok": True, "found": True, "auto_applied": True,
                "songs": sl["song_count"], "url": sl["url"],
                "match": q, "capture": get(cid)}

    # Found, but not certain. Hold it as a PROPOSAL and mark it for a person.
    # Writing it would be silent and durable — a wrong setlist in a file looks
    # exactly like a right one, and nothing downstream ever re-checks it.
    _update(cid, state=REVIEW, proposed_setlist=json.dumps(sl),
            match_report=json.dumps(q), next_attempt_at=None,
            last_reason=q["summary"], attempts=cap["attempts"] + 1)
    return {"ok": True, "found": True, "auto_applied": False,
            "needs_review": True, "songs": sl["song_count"],
            "url": sl["url"], "match": q, "capture": get(cid)}


def _apply_setlist(cid: str, cap: dict, sl: dict, state: str,
                   match: dict | None = None) -> None:
    """Store a setlist without ever stepping on a human correction."""
    ov = cap["overrides"]
    fields = {
        "setlist": json.dumps(sl),
        "setlist_url": sl.get("url"),
        "state": state,
        "enriched_at": _now(),
        "next_attempt_at": None,
        "last_reason": None,
        "proposed_setlist": None,
        "match_report": json.dumps(match) if match else None,
    }
    # Only fill fields the human has NOT corrected, and only where stage 1 left
    # a blank. setlist.fm is better than nothing here; it is not better than
    # what the app knew when it recorded, and it is never better than a person.
    for src, dst in (("tour", "tour"), ("venue", "venue"), ("city", "city")):
        if ov.get(dst):
            continue
        if not cap.get(dst) and sl.get(src):
            fields[dst] = sl[src]
    _update(cid, **fields)
    write_nfo(get(cid))


def confirm(cid: str, by: str) -> dict:
    """Accept a proposed setlist that was not certain enough to auto-apply.

    This is the human standing in for the checks that could not be satisfied
    automatically, so it records WHO — an applied-but-uncertain match with no
    name on it cannot be explained later.
    """
    if not by:
        return {"ok": False, "error": "confirming a match requires a reviewer"}
    cap = get(cid)
    if not cap:
        return {"ok": False, "error": "no such capture"}
    if not cap["proposed_setlist"]:
        return {"ok": False, "error": "there is nothing waiting to be confirmed"}
    sl = cap["proposed_setlist"]
    report = dict(cap["match_report"] or {})
    report["confirmed_by"] = by
    report["confirmed_at"] = _now()
    # Lands as MANUAL, not ENRICHED: a person vouched for this, so no later
    # lookup may revisit it. ENRICHED means the machine was certain on its own.
    _apply_setlist(cid, cap, sl, MANUAL, match=report)
    return {"ok": True, "songs": sl["song_count"], "capture": get(cid)}


def decline(cid: str, by: str, reason: str = "") -> dict:
    """Reject a proposed setlist and keep looking.

    The declined id is remembered so the same wrong match is not re-proposed on
    every subsequent pass, which would make the review queue unclearable.
    """
    if not by:
        return {"ok": False, "error": "declining a match requires a reviewer"}
    cap = get(cid)
    if not cap:
        return {"ok": False, "error": "no such capture"}
    sl = cap["proposed_setlist"]
    if not sl:
        return {"ok": False, "error": "there is nothing waiting to be confirmed"}
    declined = list(cap["declined_ids"])
    if sl.get("setlist_id"):
        # id@version, so a later correction to this setlist is re-offered
        # rather than silently suppressed for good.
        declined.append(f"{sl['setlist_id']}@{sl.get('version_id')}")
    nxt, state = _schedule_next(cap)
    _update(cid, state=state, proposed_setlist=None, match_report=None,
            declined_ids=json.dumps(declined), next_attempt_at=nxt,
            last_reason=f"a proposed match was declined by {by}"
                        + (f": {reason}" if reason else ""))
    return {"ok": True, "capture": get(cid)}


def unmatched(limit: int = 500) -> list[dict]:
    """Every capture with no confirmed setlist — the mark, as a list.

    Includes both `minimal` (nothing found yet) and `review` (something found
    that nobody has confirmed), because from the library's point of view both
    mean the same thing: this recording has no setlist attached to it.
    """
    return [c for c in listing(limit=limit) if c["unmatched"]]


def enrich_pass(limit: int = 20) -> dict:
    """Scheduled sweep. Bounded per pass so it cannot monopolise the rate limit."""
    avail = setlistfm.available()
    if not avail["ok"]:
        return {"ok": True, "skipped": avail["detail"], "lookup": avail}
    applied, review, still_waiting, gave_up = 0, 0, 0, 0
    for cap in due(limit):
        r = enrich_one(cap["id"])
        if r.get("auto_applied"):
            applied += 1
        elif r.get("needs_review"):
            review += 1
        elif r.get("state") == UNAVAILABLE:
            gave_up += 1
        else:
            still_waiting += 1
    return {"ok": True, "auto_applied": applied, "tagged_for_review": review,
            "still_waiting": still_waiting, "settled_unavailable": gave_up,
            "lookup": avail}


def attach_pasted(cid: str, text: str, by: str = "", url: str = "") -> dict:
    """The manual path — always available, key or no key.

    Lands as `manual`, which is excluded from further lookups: a person typed
    this, so nothing should overwrite it later.
    """
    cap = get(cid)
    if not cap:
        return {"ok": False, "error": "no such capture"}
    eff = cap["effective"]
    sl = setlistfm.parse_pasted(text, artist=eff["artist"],
                                performance_date=eff.get("performance_date") or "",
                                venue=eff.get("venue") or "", url=url)
    if not sl["tracks"]:
        return {"ok": False, "error": "no songs found in that text"}
    _apply_setlist(cid, cap, sl, MANUAL)
    return {"ok": True, "songs": sl["song_count"], "capture": get(cid)}


def set_overrides(cid: str, by: str = "", **fields) -> dict:
    """Correct a capture. Corrections outrank both stages, permanently."""
    cap = get(cid)
    if not cap:
        return {"ok": False, "error": "no such capture"}
    allowed = ("artist", "festival", "stage", "venue", "city", "tour",
               "performance_date")
    ov = dict(cap["overrides"])
    changed_artist_or_date = False
    for k, v in fields.items():
        if v is None:
            continue
        if k not in allowed:
            return {"ok": False, "error": f"{k} is not correctable"}
        if str(v).strip() == "":
            ov.pop(k, None)
        else:
            ov[k] = str(v).strip()
        if k in ("artist", "performance_date"):
            changed_artist_or_date = True

    upd = {"overrides": json.dumps(ov)}
    if changed_artist_or_date and cap["state"] in (MINIMAL, UNAVAILABLE):
        # The lookup key itself changed, so a previous "no setlist posted" says
        # nothing about the corrected artist or date. Reopen the search — a
        # capture that gave up under a wrong name would otherwise stay
        # `unavailable` forever for a show that is on setlist.fm.
        upd.update({"state": MINIMAL, "attempts": 0,
                    "next_attempt_at": int(time.time()),
                    "last_reason": "reopened: lookup key was corrected"})
    _update(cid, **upd)
    out = get(cid)
    write_nfo(out)
    return {"ok": True, "capture": out}


# --------------------------------------------------------------------------
# writing it out
# --------------------------------------------------------------------------

def _plot(cap: dict) -> str:
    """The visible payoff: what Plex actually shows on the episode.

    Stage 1 produces a real description on its own. When a setlist arrives it is
    appended as a numbered list, which is the form people read setlists in and
    the only place Plex will surface them for a video item.
    """
    eff, lines = cap["effective"], []
    where = " · ".join(x for x in (eff.get("festival"), eff.get("stage"),
                                   eff.get("venue"), eff.get("city")) if x)
    head = f"{eff['artist']} live"
    if where:
        head += f" at {where}"
    if eff.get("performance_date"):
        head += f", {eff['performance_date']}"
    lines.append(head + ".")
    if eff.get("tour"):
        lines.append(f"{eff['tour']}.")

    sl = cap.get("setlist")
    if sl and sl.get("tracks"):
        lines.append("")
        lines.append("Setlist:")
        enc = None
        for t in sl["tracks"]:
            if t.get("encore") and t["encore"] != enc:
                enc = t["encore"]
                lines.append(f"— Encore {enc} —" if enc > 1 else "— Encore —")
            row = f"{t['position']}. {t['title']}"
            if t.get("cover_of"):
                row += f" ({t['cover_of']} cover)"
            if t.get("with"):
                row += f" (with {t['with']})"
            if t.get("segue_into_next"):
                row += " >"
            lines.append(row)
        # Attribution is a CONDITION of setlist.fm's licence, not a courtesy, so
        # it goes into the artifact itself — where it survives the app being
        # rebuilt, moved or handed to someone else — rather than into a config
        # file or a UI footer that does not travel with the file.
        if sl.get("source") == "setlist.fm":
            lines.append("")
            lines.append(setlistfm.ATTRIBUTION
                         + (f" — {sl['url']}" if sl.get("url") else ""))
        elif sl.get("url"):
            lines.append("")
            lines.append(f"Setlist: {sl['url']}")
    elif cap.get("proposed_setlist"):
        # A setlist WAS found and is waiting on a person. Saying "none has been
        # posted" here would be a different claim and a false one — and it is the
        # claim that would send someone looking for a setlist that already exists.
        prop = cap["proposed_setlist"]
        why = (cap.get("match_report") or {}).get("summary", "")
        lines.append("")
        lines.append(f"A {prop.get('song_count', 0)}-song setlist was found but "
                     "is awaiting review, so it has not been applied.")
        if why:
            lines.append(why)
        if prop.get("url"):
            lines.append(prop["url"])
    else:
        lines.append("")
        lines.append("No setlist has been posted for this performance.")
    return "\n".join(lines)


def _write_album_nfo(cap: dict, folder: Path) -> str | None:
    """One album.nfo for an audio capture. Never fails the caller.

    Wrapped because a sidecar is documentation: if the folder is not writable
    (files put there by a different user, a read-only mount), that is worth
    reporting but must not take down an API request or a scheduled pass.
    """
    eff = cap["effective"]
    root = ET.Element("album")

    def sub(tag: str, text) -> None:
        if text not in (None, ""):
            ET.SubElement(root, tag).text = str(text)

    sub("title", eff.get("festival") or folder.name)
    sub("artist", eff["artist"])
    sub("albumartist", eff["artist"])
    sub("year", (eff.get("performance_date") or "")[:4])
    sub("releasedate", eff.get("performance_date"))
    sub("venue", eff.get("venue"))
    sub("tour", eff.get("tour"))
    sub("genre", "Live")
    sub("plot", _plot(cap))
    if cap.get("setlist_url"):
        sub("source", cap["setlist_url"])
    sl = cap.get("setlist") or {}
    for t in sl.get("tracks", []):
        tr = ET.SubElement(root, "track")
        ET.SubElement(tr, "position").text = str(t["position"])
        ET.SubElement(tr, "title").text = t["title"]
    sub("uniqueid", cap["id"])
    dest = folder / "album.nfo"
    try:
        ET.ElementTree(root).write(dest, encoding="utf-8", xml_declaration=True)
    except OSError:
        return None
    return str(dest)


def write_nfo(cap: dict) -> str | None:
    """Regenerate the sidecar from the DB. Safe to call repeatedly.

    The DATABASE is authoritative, not the file — so enrichment and corrections
    both just rewrite it. Trying to merge new data into an existing NFO would
    mean parsing back something a person may have hand-edited, and then guessing
    which side wins; regenerating makes that question not exist.
    """
    if not cap or not cap.get("file_path"):
        return None
    video = Path(cap["file_path"])
    if not video.exists():
        return None

    # An AUDIO capture is an album of many files, not one video. Writing an
    # <episodedetails> sidecar next to every track is doubly wrong: that shape
    # describes a TV episode, and Plex music libraries ignore NFO altogether.
    # So audio gets ONE album.nfo in the album folder, which is at least a true
    # human-readable record of the show even though Plex will not read it.
    if video.suffix.lower() in (".m4a", ".mp3", ".flac", ".opus", ".ogg", ".wav"):
        return _write_album_nfo(cap, video.parent)
    eff = cap["effective"]
    root = ET.Element("episodedetails")

    def sub(tag: str, text) -> None:
        if text in (None, ""):
            return
        ET.SubElement(root, tag).text = str(text)

    title = eff["artist"]
    if eff.get("festival"):
        title = f"{eff['artist']} — {eff['festival']}"
    sub("title", title)
    sub("showtitle", cap.get("show") or eff.get("festival"))
    sub("plot", _plot(cap))
    sub("aired", eff.get("performance_date"))
    sub("premiered", eff.get("performance_date"))
    if cap.get("starts_at") and cap.get("ends_at"):
        sub("runtime", max(1, (cap["ends_at"] - cap["starts_at"]) // 60))
    sub("studio", eff.get("festival"))
    sub("genre", "Music")
    for t in ("Live", "Concert"):
        ET.SubElement(root, "tag").text = t
    if eff.get("venue"):
        ET.SubElement(root, "tag").text = eff["venue"]
    if cap.get("setlist_url"):
        sub("source", cap["setlist_url"])
    sl = cap.get("setlist") or {}
    if sl.get("source") == "setlist.fm":
        # A machine-readable credit as well as the human-readable one in the
        # plot, so the obligation is met even where a reader only parses tags.
        ET.SubElement(root, "tag").text = setlistfm.ATTRIBUTION
    # Enough to reconstruct where this came from without the database.
    sub("uniqueid", cap["id"])

    dest = video.with_suffix(".nfo")
    try:
        ET.ElementTree(root).write(dest, encoding="utf-8", xml_declaration=True)
    except OSError:
        # Metadata is not worth failing a request over — the recording is the
        # valuable part and it is already safely in the library.
        return None
    return str(dest)
