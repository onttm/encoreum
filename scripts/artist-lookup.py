#!/usr/bin/env python3
"""
artist-lookup.py
=========================================================================
Given an artist name — typically one line of a festival schedule — answer:

    * Who is this, canonically?      (MusicBrainz ID, via Lidarr)
    * What genre is it?              (MusicBrainz genre tags)
    * What does the server hold?     (Plex: audio, music video, concerts)

The join is the point. "My Plex has 5 albums, no live footage" is exactly
the signal that makes a festival set worth recording, and it needs all three
answers at once.

Phrasing is deliberately about the SERVER, not the reader — roughly 18 people
share this library, so "you own" would be false for most of them. Set
LIBRARY_NAME to rename it.

WHY NO NEW API KEYS
    Everything here uses services already running on this host with credentials
    already on disk:

      Lidarr  — proxies MusicBrainz. Returns MBID, genres, artist type AND a
                disambiguation string, which is what separates "Band C (American
                rock band)" from "Band C (90s trance DJ Thomas Buettner)".
                Works for artists NOT in the library, which is most of a lineup.
      Plex    — /hubs/search answers audio + video + concerts in ONE call.

    Last.fm / Spotify / Discogs were considered and are not needed unless you
    want bios and images. Do not add a key you do not have a use for.

WHEN THE MATCH IS WRONG — the escape hatch
    Automatic matching WILL get some artists wrong. It is name-based, and names
    are ambiguous: "Levity" is a Polish jazz band, an EDM trio AND a Brighton
    4-piece, and only festival context distinguishes them. So there must always
    be a way for a human to correct it and have the correction STICK.

    That is what OVERRIDES_FILE is. A JSON map of query-name -> the answer:

        {
          "levity":  {"mbid": "…", "name": "Levity",
                      "note": "EDM trio — the Coachella booking"},
          "the who": {"mbid": "…", "name": "The Who"}
        }

    An override short-circuits ranking entirely and is reported as such, so a
    corrected match never looks like a lucky guess. Corrections persist and are
    reused, which is the whole point — fixing the same name twice is a bug.

    TODO(ui): the browse page needs a "Fix match" control next to any resolved
    artist — the Plex / Sonarr / Radarr affordance. It should write into this
    same file (or its table equivalent) so CLI and UI agree. Consider scoping
    overrides per festival later: "Levity" at Coachella is the EDM trio, at a
    jazz festival it very likely is not.

MATCH RANKING (best first)
    1. already tracked in Lidarr   — the server has them; strongest signal
    2. exact case-insensitive name AND has genre tags
    3. exact name
    4. has genre tags
    Ties break toward the first result, which is MusicBrainz's own relevance
    order. The disambiguation string is always printed so a wrong pick is
    obvious rather than silent.

USAGE
    ./artist-lookup.py "Four Tet"
    ./artist-lookup.py --file lineup.txt         # one artist per line
    ./artist-lookup.py "Band C" "Example Artist" --json

CREDENTIALS (read automatically; override with env)
    LIDARR_URL      default http://localhost:8686
    LIDARR_API_KEY  else read from appdata/lidarr/config.xml
    PLEX_URL        default http://localhost:32400
    PLEX_TOKEN      else read from Plex Preferences.xml
    (Reading the config files needs root; env vars avoid that.)

Standalone: Python 3 stdlib only. No Claude, no new API keys.
=========================================================================
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# The library is shared by ~18 people, so output is phrased about the SERVER
# ("My Plex has 6 albums") rather than about the reader ("you own 6 albums").
# Whoever is looking, the sentence stays true.
LIBRARY_NAME = os.environ.get("LIBRARY_NAME", "the Plex server")

LIDARR_URL = os.environ.get("LIDARR_URL", "http://localhost:8686").rstrip("/")


def _secret(name: str) -> str:
    """Same _FILE convention the app uses: file first, env var as fallback.

    Duplicated here rather than imported because this script must keep working
    standalone, outside the container and without the app package.
    """
    for path in (os.environ.get(f"{name}_FILE", ""), f"/run/secrets/{name.lower()}"):
        if not path:
            continue
        try:
            v = open(path, encoding="utf-8").read().strip()
            if v:
                return v
        except OSError:
            continue
    return os.environ.get(name, "").strip()
PLEX_URL = os.environ.get("PLEX_URL", "http://localhost:32400").rstrip("/")

# Human corrections live here and win over any automatic ranking.
OVERRIDES_FILE = os.environ.get(
    "ARTIST_OVERRIDES",
    os.path.join(os.environ.get("CONFIG_DIR", "/config"), "artist-overrides.json"))

LIDARR_CFG = "/home/you/docker/appdata/lidarr/config.xml"
PLEX_PREFS = ("/home/you/docker/appdata/plex/Library/Application Support/"
              "Plex Media Server/Preferences.xml")

# Plex hub types, grouped the way a person thinks about them rather than the
# way Plex stores them.
AUDIO_HUBS = {"artist", "album", "track"}
VIDEO_HUBS = {"movie", "show", "episode", "clip"}


def _read_lidarr_key() -> str:
    if _secret("LIDARR_API_KEY"):
        return os.environ["LIDARR_API_KEY"]
    try:
        m = re.search(r"<ApiKey>([^<]+)</ApiKey>", open(LIDARR_CFG).read())
        return m.group(1) if m else ""
    except OSError:
        return ""


def _read_plex_token() -> str:
    if _secret("PLEX_TOKEN"):
        return os.environ["PLEX_TOKEN"]
    try:
        m = re.search(r'PlexOnlineToken="([^"]+)"', open(PLEX_PREFS).read())
        return m.group(1) if m else ""
    except OSError:
        return ""


def _get(url: str, headers: dict | None = None, timeout: int = 30) -> bytes | None:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# Lidarr / MusicBrainz
# --------------------------------------------------------------------------
def load_overrides() -> dict:
    """Human-corrected matches. Missing file is normal, not an error."""
    try:
        with open(OVERRIDES_FILE) as f:
            return {k.strip().lower(): v for k, v in json.load(f).items()}
    except (OSError, ValueError):
        return {}


def save_override(name: str, mbid: str, artist_name: str, note: str = "") -> bool:
    """Record a correction so it is never made twice."""
    cur = {}
    try:
        with open(OVERRIDES_FILE) as f:
            cur = json.load(f)
    except (OSError, ValueError):
        pass
    cur[name.strip().lower()] = {"mbid": mbid, "name": artist_name, "note": note}
    try:
        os.makedirs(os.path.dirname(OVERRIDES_FILE), exist_ok=True)
        with open(OVERRIDES_FILE, "w") as f:
            json.dump(cur, f, indent=2, sort_keys=True)
        return True
    except OSError as e:
        print(f"could not write {OVERRIDES_FILE}: {e}", file=sys.stderr)
        return False


def lidarr_lookup(name: str, key: str) -> list[dict]:
    if not key:
        return []
    q = urllib.parse.urlencode({"term": name})
    raw = _get(f"{LIDARR_URL}/api/v1/artist/lookup?{q}", {"X-Api-Key": key}, 45)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def rank_candidates(name: str, cands: list[dict]) -> list[dict]:
    """Score candidates so the disambiguation decision is explicit, not implicit."""
    want = name.strip().lower()
    scored = []
    for i, c in enumerate(cands):
        nm = (c.get("artistName") or "").strip().lower()
        owned = (c.get("id") or 0) > 0
        genres = c.get("genres") or []
        score = 0
        if owned:
            score += 100          # you already track them — decisive
        if nm == want:
            score += 40
        if genres:
            score += 20
        score -= i                # preserve MusicBrainz relevance order on ties
        scored.append((score, c))
    scored.sort(key=lambda t: -t[0])
    return [c for _, c in scored]


# --------------------------------------------------------------------------
# Plex
# --------------------------------------------------------------------------
def plex_holdings(name: str, token: str) -> dict:
    """One /hubs/search call -> what the library holds, split audio vs video."""
    # `sections` is a LIST from the outset. It used to be a set converted only
    # on the success path, so every early return (no token, no response, parse
    # error) produced an object json.dumps could not encode — which took down
    # the caller rather than degrading.
    out = {"audio": [], "video": [], "sections": []}
    seen_sections: set[str] = set()
    if not token:
        return out
    q = urllib.parse.urlencode({"query": name, "limit": 12, "X-Plex-Token": token})
    raw = _get(f"{PLEX_URL}/hubs/search?{q}", timeout=30)
    if not raw:
        return out
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return out
    want = name.strip().lower()
    for hub in root.findall("Hub"):
        htype = hub.get("type") or ""
        for node in list(hub):
            title = node.get("title") or node.get("grandparentTitle") or ""
            gp = (node.get("grandparentTitle") or node.get("parentTitle") or "")
            # Only keep hits actually about this artist. Plex's fuzzy search
            # happily returns an "actor" hub and unrelated titles.
            hay = f"{title} {gp}".lower()
            if want not in hay:
                continue
            sect = node.get("librarySectionTitle") or hub.get("librarySectionTitle") or ""
            item = {"type": htype, "title": title[:70], "section": sect}
            if htype in AUDIO_HUBS:
                out["audio"].append(item)
            elif htype in VIDEO_HUBS:
                out["video"].append(item)
            if sect:
                seen_sections.add(sect)
    out["sections"] = sorted(seen_sections)
    return out


def plex_artist_meta(name: str, token: str) -> dict:
    """Plex's own genre/style for an owned artist — richer than Genre alone."""
    if not token:
        return {}
    q = urllib.parse.urlencode({"query": name, "X-Plex-Token": token})
    raw = _get(f"{PLEX_URL}/hubs/search?{q}", timeout=30)
    if not raw:
        return {}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return {}
    key = None
    for hub in root.findall("Hub"):
        if hub.get("type") == "artist":
            d = hub.find("Directory")
            if d is not None:
                key = d.get("ratingKey")
            break
    if not key:
        return {}
    raw = _get(f"{PLEX_URL}/library/metadata/{key}?"
               + urllib.parse.urlencode({"X-Plex-Token": token}), timeout=30)
    if not raw:
        return {}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return {}
    d = root.find("Directory")
    if d is None:
        return {}
    return {
        "genres": [g.get("tag") for g in d.findall("Genre")],
        "styles": [g.get("tag") for g in d.findall("Style")],
        "country": [g.get("tag") for g in d.findall("Country")],
        "has_bio": bool((d.get("summary") or "").strip()),
    }


def lookup(name: str, lk: str, pt: str, overrides: dict | None = None) -> dict:
    overrides = load_overrides() if overrides is None else overrides
    cands = rank_candidates(name, lidarr_lookup(name, lk))

    ov = overrides.get(name.strip().lower())
    corrected = False
    if ov and ov.get("mbid"):
        # A human said so. Prefer the full candidate record if it is in the
        # result set (keeps genres); otherwise synthesise from the override.
        match = next((c for c in cands
                      if (c.get("foreignArtistId") or "") == ov["mbid"]), None)
        best = match or {"artistName": ov.get("name") or name,
                         "foreignArtistId": ov["mbid"],
                         "disambiguation": ov.get("note", ""), "genres": []}
        cands = [best] + [c for c in cands if c is not best]
        corrected = True
    else:
        best = cands[0] if cands else {}
    hold = plex_holdings(name, pt)
    pmeta = plex_artist_meta(name, pt) if hold["audio"] else {}
    return {
        "query": name,
        "corrected": corrected,
        "matched": best.get("artistName"),
        "mbid": best.get("foreignArtistId"),
        "type": best.get("artistType"),
        "disambiguation": best.get("disambiguation") or "",
        "owned_in_lidarr": (best.get("id") or 0) > 0,
        "genres": best.get("genres") or [],
        "alternatives": [
            {"name": c.get("artistName"), "disambiguation": c.get("disambiguation") or "",
             "mbid": (c.get("foreignArtistId") or "")[:8]}
            for c in cands[1:3]],
        "plex": {
            "audio": hold["audio"], "video": hold["video"],
            "sections": hold["sections"],
            "styles": pmeta.get("styles", []), "plex_genres": pmeta.get("genres", []),
        },
    }


def render(r: dict) -> None:
    print(f"\n{'=' * 72}")
    print(f"  {r['query']}")
    print(f"{'=' * 72}")
    if not r["matched"]:
        print("  no match in MusicBrainz (via Lidarr)")
        return
    own = f"  [tracked in Lidarr]" if r["owned_in_lidarr"] else ""
    fix = "  [MANUALLY CORRECTED]" if r.get("corrected") else ""
    print(f"  matched : {r['matched']}{own}{fix}")
    if r["disambiguation"]:
        print(f"            ({r['disambiguation']})")
    print(f"  mbid    : {r['mbid']}   type: {r['type']}")
    g = r["genres"]
    print(f"  genre   : {', '.join(g[:8]) if g else '(none in MusicBrainz)'}")
    if r["plex"]["styles"]:
        print(f"  plex    : {', '.join(r['plex']['styles'][:6])}")
    if r["alternatives"]:
        print("  other candidates:")
        for a in r["alternatives"]:
            print(f"      - {a['name']} ({a['disambiguation'] or 'no disambiguation'})")
        if not r.get("corrected"):
            print(f"      fix with: {sys.argv[0]} --fix \"{r['query']}\" <mbid>")

    aud, vid = r["plex"]["audio"], r["plex"]["video"]
    print(f"\n  ON {LIBRARY_NAME.upper()}")
    if not aud and not vid:
        print(f"      nothing — not in the library")
    if aud:
        kinds = {}
        for i in aud:
            kinds[i["type"]] = kinds.get(i["type"], 0) + 1
        print(f"      audio : {', '.join(f'{v} {k}' for k, v in kinds.items())}")
        for i in aud[:3]:
            print(f"                {i['title'][:56]}")
    if vid:
        kinds = {}
        for i in vid:
            kinds[i["type"]] = kinds.get(i["type"], 0) + 1
        print(f"      video : {', '.join(f'{v} {k}' for k, v in kinds.items())}")
        for i in vid[:3]:
            print(f"                {i['title'][:56]}")

    # The line that actually drives a decision. Counts are specific because
    # "some albums" does not help anyone choose.
    albums = sum(1 for i in aud if i["type"] == "album")
    tracks = sum(1 for i in aud if i["type"] == "track")
    shows = sum(1 for i in vid if i["type"] in ("show", "episode", "movie", "clip"))
    a_txt = (f"{albums} album{'s' if albums != 1 else ''}" if albums
             else (f"{tracks} track{'s' if tracks != 1 else ''}" if tracks else ""))
    print()
    if aud and not vid:
        print(f"  -> {LIBRARY_NAME} has {a_txt}, no live footage. "
              "Good candidate to record.")
    elif not aud and not vid:
        print(f"  -> {LIBRARY_NAME} has nothing for this artist.")
    elif vid and not aud:
        print(f"  -> {LIBRARY_NAME} has {shows} video item{'s' if shows != 1 else ''} "
              "but no albums.")
    else:
        print(f"  -> {LIBRARY_NAME} has {a_txt} and {shows} video "
              f"item{'s' if shows != 1 else ''} — check it is not the same show.")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("artists", nargs="*", help="artist name(s)")
    ap.add_argument("--file", help="file with one artist per line")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fix", nargs=2, metavar=("NAME", "MBID"),
                    help="record a human correction: the given name always "
                         "resolves to this MusicBrainz id from now on")
    ap.add_argument("--fix-note", default="", help="why, for whoever reads it later")
    ap.add_argument("--list-fixes", action="store_true")
    args = ap.parse_args()

    if args.list_fixes:
        ovs = load_overrides()
        if not ovs:
            print(f"no overrides in {OVERRIDES_FILE}")
        for k, v in sorted(ovs.items()):
            print(f"  {k:<24} -> {v.get('name','?')} ({v.get('mbid','')[:8]}…) "
                  f"{v.get('note','')}")
        return 0

    if args.fix:
        nm, mb = args.fix
        cands = lidarr_lookup(nm, _read_lidarr_key())
        hit = next((c for c in cands if (c.get("foreignArtistId") or "") == mb), None)
        ok = save_override(nm, mb, (hit or {}).get("artistName") or nm, args.fix_note)
        print(f"{'saved' if ok else 'FAILED'}: {nm!r} -> "
              f"{(hit or {}).get('artistName') or mb}")
        return 0 if ok else 1

    names = list(args.artists)
    if args.file:
        try:
            names += [l.strip() for l in open(args.file) if l.strip()
                      and not l.startswith("#")]
        except OSError as e:
            sys.exit(f"cannot read {args.file}: {e}")
    if not names:
        ap.error("give at least one artist name, or --file")

    lk, pt = _read_lidarr_key(), _read_plex_token()
    if not lk:
        print("warning: no Lidarr API key — genre lookup disabled", file=sys.stderr)
    if not pt:
        print("warning: no Plex token — library check disabled", file=sys.stderr)

    results = [lookup(n, lk, pt) for n in names]
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            render(r)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
