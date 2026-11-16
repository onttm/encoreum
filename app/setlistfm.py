"""
setlistfm.py -- setlist lookup, when and if one exists.

WHAT THIS IS FOR
    A festival set is recorded the moment it airs. Nobody has posted its setlist
    yet — setlists are written up by attendees AFTERWARDS, typically hours to
    days later, and plenty of sets never get one at all. So the capture cannot
    wait for it, and the metadata has to arrive in two stages:

        at capture   artist, festival, stage, date, set times, stream
        later        song-by-song setlist, tour name, confirmed venue

    This module is the second stage. It is strictly an UPGRADE path: the file is
    complete, correctly named and playable without it.

OPTIONAL BY CONSTRUCTION
    No API key is required for this app to work. With a key, lookups happen on a
    schedule; without one, the paste path does the same job by hand and returns
    exactly the same structure. Nothing silently breaks and nothing degrades to
    a worse-but-still-running state — `available()` says which mode is in force
    and the UI is expected to show it.

    (Global rule: a shipped tool must not require a service to function. Here
    the service is setlist.fm rather than an LLM, but the reasoning is the same.)

THE DATE TRAP
    setlist.fm files a setlist under the date the show was PLAYED, never the
    date it was broadcast. For festivals those differ constantly — sets air
    delayed, get replayed the next day, and weekend-two performances are often
    streamed from weekend one. Searching by air date silently returns nothing,
    or worse, returns the wrong night's setlist for the right artist.

    So callers must pass the PERFORMANCE date. For a live festival stream that
    is the segment's local start date at the festival's timezone — not the
    capture's timestamp, which is UTC and may already have rolled over.

    The API's own date format is dd-MM-yyyy. Not ISO. Getting this wrong returns
    an empty result rather than an error, which is the kind of failure that
    looks like "no setlist exists yet" forever.

TERMS AND RATE LIMITS
    setlist.fm's API is free for NON-COMMERCIAL use and requires attribution.
    This app is a personal archiving tool, which is squarely inside that.

    RATE LIMIT: max 2.0 requests/second, max 1,440 requests/day. Published on
    the API-key page at setlist.fm/settings/api, which requires a login — the
    PUBLIC docs state no figures, and reading only those produced a confident
    "setlist.fm publishes no rate limit" that was simply wrong. An
    unauthenticated check cannot prove a limit is unpublished.

    1,440/day is exactly one request per minute sustained. Our own budget sits
    under theirs (see app/ratelimit.py) so we trip our ceiling first.

    Attribution is kept in code rather than in a policy document: every stored
    setlist keeps the `url` it came from, so the credit survives into the
    metadata itself.

    Their guidelines also exclude "tape" entries — recorded music played over
    the PA between or during sets is not a performance. Those are dropped, for
    the same reason ingest-taper.py drops "Intro" and "Band Intros": they are
    not songs the artist played.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime

from . import ratelimit, secrets

API = "https://api.setlist.fm/rest/1.0"

# Attribution is a CONDITION of the free API licence, not a courtesy. It is a
# module constant so every surface that shows a setlist uses the same wording,
# and so it travels into the NFO with the data rather than living in a config
# file that can be lost when the app is rebuilt or handed on.
ATTRIBUTION = "Setlist data from setlist.fm"
UA = "encoreum/1.0 (+personal media archiving; non-commercial)"

SERVICE = "setlistfm"          # limits live in app/ratelimit.py

_lock = threading.Lock()
_backoff_until = 0.0


SERVICE_KEY = "SETLISTFM_API_KEY"


def api_key() -> str:
    return secrets.read(SERVICE_KEY)


def key_source() -> str:
    return secrets.source_of(SERVICE_KEY)


def available() -> dict:
    """Which mode is in force, in a form the UI can render honestly."""
    budget = ratelimit.status(SERVICE)
    if _backoff_until > time.time():
        return {"mode": "backoff", "ok": False, "budget": budget,
                "detail": f"setlist.fm returned 429; backing off for another "
                          f"{int(_backoff_until - time.time())}s"}
    if api_key() and budget.get("exhausted"):
        # Not an error and not a broken integration — a deliberately
        # conservative self-imposed budget doing its job.
        return {"mode": "budget_spent", "ok": False, "budget": budget,
                "detail": f"the self-imposed daily budget of "
                          f"{budget['daily_cap']} requests is spent; it refills "
                          "as the rolling 24h window moves. Setlists can still "
                          "be pasted in by hand."}
    if api_key():
        return {"mode": "api", "ok": True, "budget": budget,
                "key_source": key_source(),
                "detail": f"setlist.fm lookups run automatically "
                          f"({budget['remaining']} of {budget['daily_cap']} "
                          "requests left in the rolling 24h budget)"}
    return {"mode": "manual", "ok": False, "budget": budget,
            "key_source": key_source(),
            "detail": "no setlist.fm API key available — captures keep their basic "
                      "metadata, and a setlist can be pasted in by hand at any "
                      "time. Nothing is blocked by this."}


def _get(path: str, **params) -> dict | None:
    global _backoff_until
    key = api_key()
    if not key:
        return None
    if time.time() < _backoff_until:
        return None
    ok, why = ratelimit.acquire(SERVICE)
    if not ok:
        # Budget spent. Skip cleanly — the caller reschedules and tries later.
        return None
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "x-api-key": key, "Accept": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # Not an error. "No setlist has been posted for this show" is the
            # single most common outcome and must not look like a failure, or
            # every unposted festival set reads as something to fix.
            return {"_none": True}
        if e.code == 429:
            _backoff_until = time.time() + 3600
        return None
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# normalising
# --------------------------------------------------------------------------

def _api_date(d: str) -> str:
    """ISO YYYY-MM-DD -> setlist.fm's dd-MM-yyyy."""
    return datetime.strptime(d, "%Y-%m-%d").strftime("%d-%m-%Y")


def normalise(sl: dict) -> dict:
    """Flatten one setlist.fm setlist into the shape the tagger already speaks.

    Deliberately the SAME shape parse_supplied_setlist() produces in
    ingest-taper.py, so a fetched setlist and a hand-pasted one are
    interchangeable everywhere downstream. Two sources, one structure.
    """
    tracks, pos = [], 0
    for st in (sl.get("sets") or {}).get("set") or []:
        encore = st.get("encore")
        for song in st.get("song") or []:
            name = (song.get("name") or "").strip()
            if not name:
                continue
            if song.get("tape"):
                # Recorded music over the PA. Not a performance — setlist.fm's
                # own guidelines treat it as such, and it would otherwise show
                # up as a track the artist never played.
                continue
            pos += 1
            cover = (song.get("cover") or {}).get("name")
            tracks.append({
                "position": pos,
                "title": name,
                "encore": int(encore) if encore else None,
                "cover_of": cover,
                "with": (song.get("with") or {}).get("name"),
                "info": song.get("info"),
                "segue_into_next": bool(song.get("segue")),
            })
    venue = sl.get("venue") or {}
    city = venue.get("city") or {}
    iso = ""
    if sl.get("eventDate"):
        try:
            iso = datetime.strptime(sl["eventDate"], "%d-%m-%Y").strftime("%Y-%m-%d")
        except ValueError:
            iso = ""
    return {
        "setlist_id": sl.get("id"),
        # setlist.fm is a WIKI: every edit mints a new versionId while the
        # setlist id stays put. Their docs are explicit that comparing
        # versionIds is the only reliable way to tell two setlists apart. This
        # is what lets a DECLINED match be re-offered once someone corrects it,
        # and an empty stub be noticed when it is finally filled in — keying on
        # the setlist id alone makes both of those permanent.
        "version_id": sl.get("versionId"),
        "url": sl.get("url"),                 # attribution; keep it
        "artist": (sl.get("artist") or {}).get("name"),
        "artist_mbid": (sl.get("artist") or {}).get("mbid"),
        "date": iso,
        "venue": venue.get("name"),
        "city": ", ".join(x for x in (city.get("name"),
                                      (city.get("state") or ""),
                                      (city.get("country") or {}).get("name")) if x),
        "tour": (sl.get("tour") or {}).get("name"),
        "festival": (sl.get("info") or None),
        "tracks": tracks,
        "song_count": len(tracks),
        "source": "setlist.fm",
    }


# --------------------------------------------------------------------------
# lookup
# --------------------------------------------------------------------------

def find(artist: str, performance_date: str, mbid: str = "",
         venue_hint: str = "") -> dict:
    """Look up ONE performance. `performance_date` is when it was PLAYED (ISO).

    Returns {"found": bool, ...}. `found=False` with no error is the normal,
    expected answer for a set nobody has written up yet.
    """
    if not artist or not performance_date:
        return {"found": False, "reason": "need an artist and a performance date"}
    if not api_key():
        return {"found": False, "reason": "no API key; paste a setlist instead",
                "manual": True}
    try:
        d = _api_date(performance_date)
    except ValueError:
        return {"found": False, "reason": f"bad date {performance_date!r}, "
                                          "expected YYYY-MM-DD"}

    # mbid is exact; artistName is fuzzy and will happily match a different band
    # with the same name. Prefer the mbid whenever artist-lookup resolved one.
    params = {"date": d, "p": 1}
    if mbid:
        params["artistMbid"] = mbid
    else:
        params["artistName"] = artist

    r = _get("/search/setlists", **params)
    if r is None:
        return {"found": False, "reason": "lookup unavailable", "retry": True}
    if r.get("_none"):
        return {"found": False, "reason": "no setlist posted yet", "retry": True}

    cands = r.get("setlist") or []
    if not cands:
        return {"found": False, "reason": "no setlist posted yet", "retry": True}

    picked = [normalise(s) for s in cands]
    total_candidates = len(picked)
    # An artist can play twice in a day (a festival set plus a late-night club
    # show). Prefer a venue match when we have one, then the fuller setlist —
    # an empty stub setlist exists for many shows and carries no songs at all.
    if venue_hint:
        vh = venue_hint.lower()
        picked.sort(key=lambda p: (vh not in (p["venue"] or "").lower(),
                                   -p["song_count"]))
    else:
        picked.sort(key=lambda p: -p["song_count"])
    best = picked[0]

    if best["song_count"] == 0:
        # A stub: the show is listed but no songs recorded. Worth retrying —
        # these get filled in later, and the versionId will have moved when
        # they are.
        return {"found": False, "reason": "setlist exists but is still empty",
                "retry": True, "url": best["url"],
                "version_id": best.get("version_id")}
    return {"found": True, "setlist": best,
            "candidates": total_candidates,
            "matched_by": "mbid" if mbid else "name",
            "alternatives": [{"venue": p["venue"], "songs": p["song_count"],
                              "url": p["url"]} for p in picked[1:4]]}


def parse_pasted(text: str, artist: str = "", performance_date: str = "",
                 venue: str = "", url: str = "") -> dict:
    """The manual path: turn a pasted setlist into the same structure.

    Always available, with or without a key. Accepts a plain list of songs, one
    per line, optionally numbered, with '>' segue markers and an 'Encore:'
    header — which is how setlist.fm renders them on the page a person is
    copying from.
    """
    import re
    tracks, pos, encore = [], 0, None
    for raw in (text or "").splitlines():
        line = raw.strip().strip("|").strip()
        if not line:
            continue
        m = re.match(r"^encore\s*(\d*)\s*:?\s*$", line, re.I)
        if m:
            encore = int(m.group(1)) if m.group(1) else (encore or 0) + 1
            continue
        line = re.sub(r"^\s*\d{1,2}\s*[.):\-]\s*", "", line)
        segue = bool(re.search(r"[>→]\s*$", line))
        line = re.sub(r"\s*[>→]\s*$", "", line).strip()
        cover = None
        m = re.search(r"\((?:.*?\b)?(?:cover|by)\s+([^)]+)\)\s*$", line, re.I)
        if m:
            cover = m.group(1).strip()
            line = line[:m.start()].strip()
        if not line:
            continue
        pos += 1
        tracks.append({"position": pos, "title": line, "encore": encore,
                       "cover_of": cover, "with": None, "info": None,
                       "segue_into_next": segue})
    return {"setlist_id": None, "url": url or None, "artist": artist or None,
            "artist_mbid": None, "date": performance_date or None,
            "venue": venue or None, "city": None, "tour": None, "festival": None,
            "tracks": tracks, "song_count": len(tracks), "source": "pasted"}


# --------------------------------------------------------------------------
# how certain is this match?
# --------------------------------------------------------------------------

def _norm(x: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (x or "").lower()).strip()


def match_quality(sl: dict, want: dict, candidates: int = 1,
                  matched_by: str = "name", track_count: int = 0) -> dict:
    """How certain is this match? Only `certain` is applied unattended.

    WHY THIS IS GRADED AND NOT A CHECKLIST
        The first version demanded a MusicBrainz ID match and treated a missing
        mbid as a failure. That conflated "we do not know" with "it disagrees" —
        the very distinction this function gets right for venue. Since the mbid
        is only ever populated by Lidarr enrichment, and plain downloads never
        carry one at all, that single rule would have sent almost everything to
        review permanently.

        That is not a safe default, it is a broken one. A review queue full of
        obviously-correct matches gets rubber-stamped, and a rubber-stamped
        queue catches nothing — so being too strict and being too lax fail in
        the same place, just by different routes.

    THE SHAPE THAT WORKS
        Contradictions always block. Missing facts never do on their own, but
        they do withhold the corroboration needed to reach `certain`.

        Identity can be established two ways, because there is more than one way
        to know who played:
          - the artist's MusicBrainz ID matches — identity is a fact
          - the name matches EXACTLY and the place agrees (venue, or the
            festival named in the setlist) — a different band with the same name
            playing the same venue on the same day is not a real scenario

        A name match with nothing to corroborate it is `probable`, not certain:
        "Band C" is an American rock band, a Seattle band and a 90s trance DJ, and
        setlist.fm will return whichever one it has.

    Vocabulary is shared with the ingest aligner on purpose — certain /
    probable / weak reads the same way there, and one confidence scale across
    the app is easier to reason about than two.
    """
    checks, blockers, notes = [], [], []

    def hard(name: str, ok: bool, detail: str) -> None:
        """A contradiction. Never auto-applies while this fails."""
        checks.append({"check": name, "ok": ok, "blocking": True, "detail": detail})
        if not ok:
            blockers.append(detail)

    def soft(name: str, ok: bool, detail: str) -> None:
        """Corroboration. Its absence withholds certainty but blocks nothing."""
        checks.append({"check": name, "ok": ok, "blocking": False, "detail": detail})
        if not ok:
            notes.append(detail)

    # --- contradictions -----------------------------------------------------
    want_date = (want.get("performance_date") or "").strip()
    hard("date", bool(want_date) and sl.get("date") == want_date,
         f"performance date {want_date} matches exactly"
         if sl.get("date") == want_date else
         f"setlist is dated {sl.get('date') or '?'} but the set was played "
         f"{want_date or '?'}")

    hard("content", sl.get("song_count", 0) > 0,
         f"{sl.get('song_count', 0)} songs"
         if sl.get("song_count") else "the setlist exists but has no songs in it")

    want_name, sl_name = _norm(want.get("artist")), _norm(sl.get("artist"))
    name_exact = bool(want_name) and want_name == sl_name
    name_fuzzy = bool(want_name) and not name_exact and (
        want_name in sl_name or sl_name in want_name)
    if matched_by == "mbid":
        hard("artist", True, "matched on the artist's MusicBrainz ID")
    elif name_exact:
        hard("artist", True, f"artist name matches exactly ({sl.get('artist')})")
    elif name_fuzzy:
        hard("artist", False,
             f"artist name only partly matches: recorded {want.get('artist')!r}, "
             f"setlist says {sl.get('artist')!r}")
    else:
        hard("artist", False,
             f"artist does not match: recorded {want.get('artist')!r}, setlist "
             f"says {sl.get('artist')!r}")

    known_venue, sl_venue = _norm(want.get("venue")), _norm(sl.get("venue"))
    venue_agrees = bool(known_venue and sl_venue) and (
        known_venue in sl_venue or sl_venue in known_venue)
    if known_venue and sl_venue and not venue_agrees:
        hard("venue", False,
             f"venue differs: recorded at {want.get('venue')!r}, setlist says "
             f"{sl.get('venue')!r}")
    else:
        checks.append({"check": "venue", "ok": True, "blocking": True,
                       "detail": f"venue agrees ({sl.get('venue')})" if venue_agrees
                       else "no venue on file to compare — not a disagreement"})

    # --- corroboration ------------------------------------------------------
    # The festival name often appears in setlist.fm's free-text `info` field
    # ("Coachella 2026"), which is exactly the confirmation a festival capture
    # needs when no venue was recorded.
    fest = _norm(want.get("festival"))
    fest_agrees = bool(fest) and fest.split()[0] in _norm(
        f"{sl.get('festival')} {sl.get('venue')} {sl.get('tour')}")

    identity_certain = matched_by == "mbid" or (
        name_exact and (venue_agrees or fest_agrees))
    soft("identity", identity_certain,
         "identity is confirmed by the MusicBrainz ID" if matched_by == "mbid"
         else ("the name matches exactly and the place agrees, which together "
               "identify the show") if identity_certain
         else "the artist name matches but nothing corroborates it — no venue "
              "or festival on the setlist agrees, and names are shared between "
              "acts")

    # COVERAGE. A setlist that is right about the artist, date and venue can
    # still be the WRONG HALF of the night. Found the hard way: a Band B / Guest Artist co-headline recording has 16 tracks, and setlist.fm's
    # Band B entry for that date lists 11 — because the other five open
    # Guest Artist's set and live on his entry. Every check passed, it was applied as
    # `certain`, and the NFO then claimed an 11-song setlist for a 16-track
    # album with the guest segment silently missing.
    #
    # So this is not an edge case, it is what co-headlines, guest sit-ins and
    # festival sets look like. A shortfall is not proof the match is wrong — it
    # is proof a person should look before it is written.
    if track_count:
        # Tolerance is symmetric, because BOTH directions mislead:
        #   fewer songs than tracks -> the setlist covers only part of the show
        #   more  songs than tracks -> the NFO promises songs that are not there
        # A flat percentage is too tight on short sets, so allow at least two
        # either way: medleys get merged, banter and intros get dropped.
        slack = max(2, int(round(track_count * 0.15)))
        lo, hi = track_count - slack, track_count + slack
        n = sl.get("song_count", 0)
        soft("coverage", lo <= n <= hi,
             f"{n} songs for {track_count} tracks — plausible coverage"
             if lo <= n <= hi else
             (f"the setlist has {n} songs but the recording has {track_count} "
              "tracks. It may be only PART of this recording — on a co-headline "
              "or guest segment the rest sits under the other artist"
              if n < lo else
              f"the setlist has {n} songs but the recording has only "
              f"{track_count} tracks, so the recording may be incomplete and "
              "the metadata would promise songs that are not in it"))

    soft("uniqueness", candidates == 1,
         "only one setlist exists for this artist on this date"
         if candidates == 1 else
         f"{candidates} setlists exist for this artist on this date — one may "
         "be a different show the same day")

    blocked = bool(blockers)
    if blocked:
        confidence = "weak"
    elif identity_certain and candidates == 1 and not notes:
        confidence = "certain"
    else:
        confidence = "probable"

    return {
        "confidence": confidence,
        "exact": confidence == "certain",
        "checks": checks,
        "blockers": blockers,
        "notes": notes,
        "summary": (
            "certain on every check — applied automatically" if confidence == "certain"
            else ("does not match: " + "; ".join(blockers)) if blocked
            else "probably right, but not certain: " + "; ".join(notes)),
    }
