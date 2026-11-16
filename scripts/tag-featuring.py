#!/usr/bin/env python3
"""
tag-featuring.py -- make guest artists findable, without lying about who played.

THE PROBLEM, MEASURED
    Plex indexes a track credit for search ONLY when it is a bare artist name it
    can resolve. Verified by A/B test on fresh album folders with
    `Prefer local metadata` on:

        ARTIST "Guest Artist"                        -> searchable
        ARTIST "Band B with Guest Artist"   -> NOT searchable
        ARTIST "Band B/Guest Artist"        -> NOT searchable
        ARTIST "Band B;Guest Artist"        -> NOT searchable

    So a guest appearance is invisible to search unless you credit the track to
    the guest alone — which would mean writing "Guest Artist" onto five Band B songs. Buying search by making the file lie is the wrong trade: the
    tag is the permanent record and it travels with the file.

WHAT ACTUALLY WORKS, ALSO MEASURED
        album TITLE containing "(with Guest Artist)"  -> SEARCHABLE, all accounts
        collection "Featuring Guest Artist"           -> browsable, all accounts
        playlist titled with the guest               -> searchable, but playlists
                                                        are per-account
        collections in search                        -> NOT returned, even when
                                                        populated and indexed

    Hence both, because they do different jobs: the title suffix answers "I
    typed Guest Artist into search", the collection answers "show me everything
    with a guest on it". Neither touches the per-track credit.

WHY IT EDITS PLEX, NOT THE FILES
    Two reasons, and the second is the important one:

      1. Reversible. Un-suffixing a title is one API call; rewriting tags across
         an album is not.
      2. Plex re-reads edited tags unreliably. Retagging files already ingested
         repeatedly appeared to do nothing here — section refresh, force=1,
         per-item refresh and a Plex Dance all failed to pick up changes.
         Editing through the API sidesteps that entirely.

    Fields are set LOCKED so a later scan cannot revert them.

USAGE
    ./scripts/tag-featuring.py                 # dry run, every audio library
    ./scripts/tag-featuring.py --apply
    ./scripts/tag-featuring.py --section 9 --apply --no-title
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

PLEX_URL = os.environ.get("PLEX_URL", "http://172.17.0.1:32400").rstrip("/")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")

# "X with Y", "X ft. Y", "X feat Y", "X featuring Y", "X w/ Y".
# Anchored to a separator so "Wilco" is not read as "W" featuring "ilco", and
# requiring the guest to be non-trivial.
_FEAT = re.compile(
    r"^(?P<main>.+?)\s+(?:with|w/|ft\.?|feat\.?|featuring)\s+(?P<guest>.+?)\s*$", re.I)

# Guests are split on these so "with Guest Artist & Eno" yields two names.
_SPLIT = re.compile(r"\s*(?:,|&|\band\b|\+|/|;)\s*", re.I)

# A "guest" that is really a descriptor, not a person.
_NOT_A_NAME = re.compile(
    r"^(?:the\s+)?(?:band|orchestra|choir|strings?|horns?|guests?|friends?|"
    r"special guests?|others?|more|guest vocals?)$", re.I)


def call(path: str, method: str = "GET", **params):
    if not PLEX_TOKEN:
        raise SystemExit("PLEX_TOKEN is not set — nothing to talk to.")
    params["X-Plex-Token"] = PLEX_TOKEN
    url = f"{PLEX_URL}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            body = r.read()
            return ET.fromstring(body) if body.strip().startswith(b"<") else None
    except urllib.error.HTTPError as e:
        print(f"    ! HTTP {e.code} on {method} {path}", file=sys.stderr)
        return None
    except Exception as e:  # noqa: BLE001
        print(f"    ! {type(e).__name__} on {method} {path}: {e}", file=sys.stderr)
        return None


def audio_sections() -> list[dict]:
    root = call("/library/sections")
    if root is None:
        return []
    return [{"key": d.get("key"), "title": d.get("title")}
            for d in root.findall("Directory") if d.get("type") == "artist"]


def guests_in(credit: str, album_artist: str) -> list[str]:
    """Pull guest names out of a track credit.

    Returns [] when the credit IS the album artist — the overwhelmingly common
    case, and the one that must stay cheap.
    """
    credit = (credit or "").strip()
    if not credit or credit == (album_artist or "").strip():
        return []
    m = _FEAT.match(credit)
    if not m:
        return []
    # The part before "with" should be the main act; if it is not, this is some
    # other sentence and guessing at it would invent guests.
    main = m.group("main").strip()
    if album_artist and main.lower() != album_artist.strip().lower():
        return []
    out = []
    for g in _SPLIT.split(m.group("guest")):
        g = g.strip(" ,-–—()[]")
        # Strip a trailing period ONLY when the name has no other periods.
        # "Run-D.M.C." and "R.E.M." end in one as part of the name; stripping it
        # silently renames the artist and splits them from their real entity.
        if g.endswith(".") and g.count(".") == 1:
            g = g[:-1]
        g = g.strip()
        if g and not _NOT_A_NAME.match(g) and 2 <= len(g) <= 60:
            out.append(g)
    return out


def scan(section_key: str,
         tracks_by_guest: dict[str, set[str]] | None = None) -> dict[str, dict]:
    """Album ratingKey -> {title, artist, guests}. One pass over the tracks.

    Also accumulates guest -> {track ratingKeys} across ALL sections, which is
    what the cross-library playlists are built from.
    """
    root = call(f"/library/sections/{section_key}/all", type="10")
    if root is None:
        return {}
    albums: dict[str, dict] = {}
    for t in root.findall("Track"):
        aa = t.get("grandparentTitle") or ""
        g = guests_in(t.get("originalTitle") or "", aa)
        if not g:
            continue
        rk = t.get("parentRatingKey")
        if not rk:
            continue
        a = albums.setdefault(rk, {"title": t.get("parentTitle") or "",
                                   "artist": aa, "guests": set(), "tracks": 0})
        a["guests"].update(g)
        a["tracks"] += 1
        if tracks_by_guest is not None:
            for name in g:
                tracks_by_guest.setdefault(name, set()).add(t.get("ratingKey"))
    for a in albums.values():
        a["guests"] = sorted(a["guests"])
    return albums


def collections_in(section_key: str) -> dict[str, str]:
    """name(lowercased) -> ratingKey. Checked before creating anything.

    Without this the script is not idempotent: a second run POSTs a second
    collection with the same name, and Plex happily keeps both. Observed here —
    two "Featuring Guest Artist" collections after one re-run.
    """
    root = call(f"/library/sections/{section_key}/collections")
    if root is None:
        return {}
    return {(d.get("title") or "").lower(): d.get("ratingKey")
            for d in root.findall("Directory")}


def collection_members(collection_rk: str) -> set[str]:
    root = call(f"/library/collections/{collection_rk}/children")
    if root is None:
        return set()
    return {d.get("ratingKey") for d in root if d.get("ratingKey")}


def playlists_by_title() -> dict[str, str]:
    root = call("/playlists")
    if root is None:
        return {}
    return {(p.get("title") or "").lower(): p.get("ratingKey")
            for p in root.findall("Playlist")}


def playlist_members(prk: str) -> set[str]:
    root = call(f"/playlists/{prk}/items")
    if root is None:
        return set()
    return {t.get("ratingKey") for t in root if t.get("ratingKey")}


def machine_id() -> str:
    srv = call("/")
    return srv.get("machineIdentifier") if srv is not None else ""


def suffix_for(guests: list[str]) -> str:
    return f" (with {', '.join(guests)})"


def apply_album(section_key: str, rk: str, info: dict,
                do_title: bool, do_collection: bool, apply: bool) -> None:
    title = info["title"]
    want = suffix_for(info["guests"])
    already = want.lower() in title.lower()

    if do_title and not already:
        new = title + want
        print(f"      title -> {new!r}")
        if apply:
            # PUT /library/metadata/{rk} — NOT the section-scoped
            # /library/sections/{k}/all?type=9&id=... form, which returns a bare
            # HTTP 400 here with no explanation. Locked so a rescan cannot
            # revert it.
            call(f"/library/metadata/{rk}", "PUT",
                 **{"title.value": new, "title.locked": "1"})
    elif do_title:
        print("      title already carries the guest")

    if do_collection:
        existing = collections_in(section_key)
        for g in info["guests"]:
            name = f"Featuring {g}"
            crk = existing.get(name.lower())
            if crk and rk in collection_members(crk):
                print(f"      collection {name!r} already has this album")
                continue
            srv = call("/")
            mid = srv.get("machineIdentifier") if srv is not None else ""
            uri = (f"server://{mid}/com.plexapp.plugins.library"
                   f"/library/metadata/{rk}")
            if crk:
                print(f"      collection -> {name!r} (adding to existing)")
                if apply:
                    call(f"/library/collections/{crk}/items", "PUT", uri=uri)
            else:
                print(f"      collection -> {name!r} (creating)")
                if apply:
                    # POST /library/collections with the item URI actually
                    # POPULATES it. PUT-ing a collection TAG onto the album
                    # instead leaves an EMPTY collection behind — that mistake
                    # produced a zero-item "Guest Artist" collection here.
                    call("/library/collections", "POST",
                         type="9", title=name, sectionId=section_key, smart="0",
                         uri=uri)
                    existing[name.lower()] = None   # avoid a duplicate this run


def do_playlists(tracks_by_guest: dict[str, set[str]], apply: bool,
                 min_tracks: int = 1) -> None:
    """One cross-library "Featuring X" playlist per guest.

    This is the ONLY mechanism that spans libraries. Measured on this server:

        collection + album from another library -> HTTP 400, membership unchanged
        playlist   + album from another library -> HTTP 200, items from 2 sections

    Collections are hard-scoped to one section, so a "Featuring Guest Artist"
    collection can never cover both the studio Music library and Live Music. A
    playlist can, and playlist titles ARE searchable — so this is what makes the
    convention work across an entire audio collection rather than one library.

    !! PLAYLISTS ARE PER-ACCOUNT. CONFIRMED, not suspected. !!
    Checked from a second account on this server: that user saw the album and
    the collection, and did NOT see the playlist. So a playlist cannot carry a
    convention meant for everyone — it is a private view for whoever ran this.

    Which leaves the album-TITLE suffix as the only mechanism that is BOTH
    global and searchable, and it is per-album so it never needed to span
    libraries in the first place. Prefer it. Playlists remain useful to the
    owner, and collections give everyone a browse category per library.

    Individual TRACKS are added, not whole albums. On a studio record only the
    one track has the guest on it, and adding the album would put eleven songs
    they had nothing to do with into their playlist.
    """
    mid = machine_id()
    existing = playlists_by_title()
    skipped = 0
    for guest, track_rks in sorted(tracks_by_guest.items()):
        # A playlist per one-off guest appearance is technically correct and
        # practically noise: on this library 105 of 120 guests appear on exactly
        # one track, so the default would bury the useful playlists under a
        # hundred single-item ones. The threshold is explicit rather than
        # clever, and what it drops is REPORTED — a silent cap reads as
        # "covered everything" when it did not.
        if len(track_rks) < min_tracks:
            skipped += 1
            continue
        title = f"Featuring {guest}"
        prk = existing.get(title.lower())
        have = playlist_members(prk) if prk else set()
        todo = sorted(track_rks - have)
        if not todo:
            print(f"    playlist {title!r}: already has all "
                  f"{len(track_rks)} track(s)")
            continue
        print(f"    playlist {title!r}: +{len(todo)} track(s)"
              f"{' (creating)' if not prk else ''}")
        if not apply:
            continue
        # ONE uri with comma-joined ratingKeys. Comma-joining whole URIs
        # returns HTTP 200 and silently creates an EMPTY playlist — the worst
        # kind of failure, because it looks like it worked. Measured:
        #   single uri                 -> 1 item
        #   comma-joined full uris     -> 0 items, HTTP 200
        #   one uri, comma-joined keys -> all items
        uris = (f"server://{mid}/com.plexapp.plugins.library"
                f"/library/metadata/{','.join(todo)}")
        if prk:
            call(f"/playlists/{prk}/items", "PUT", uri=uris)
        else:
            call("/playlists", "POST", type="audio", title=title,
                 smart="0", uri=uris)
            existing[title.lower()] = None
    if skipped:
        print(f"    ({skipped} guest(s) skipped: fewer than {min_tracks} "
              f"track(s) — raise/lower with --min-tracks)")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--section", action="append",
                    help="Plex section key (repeatable); default = every audio library")
    ap.add_argument("--apply", action="store_true", help="make the changes")
    ap.add_argument("--no-title", action="store_true",
                    help="skip the album-title suffix (the part that makes it "
                         "SEARCHABLE); collections alone are browse-only")
    ap.add_argument("--no-collection", action="store_true",
                    help="skip the 'Featuring X' collections")
    ap.add_argument("--min-tracks", type=int, default=1, metavar="N",
                    help="only build a playlist for guests on N+ tracks "
                         "(default 1; on a big library most guests appear once)")
    ap.add_argument("--playlists", action="store_true",
                    help="also build cross-library 'Featuring X' playlists — the "
                         "ONLY mechanism that spans libraries (collections cannot)")
    args = ap.parse_args()

    secs = ([{"key": k, "title": f"section {k}"} for k in args.section]
            if args.section else audio_sections())
    if not secs:
        print("No audio libraries found (is PLEX_TOKEN set?)")
        return 1

    total = 0
    tracks_by_guest: dict[str, set[str]] = {}
    for s in secs:
        albums = scan(s["key"], tracks_by_guest)
        print(f"\n  {s['title']} (section {s['key']}): "
              f"{len(albums)} album(s) with guest credits")
        for rk, info in sorted(albums.items(), key=lambda kv: kv[1]["title"]):
            total += 1
            print(f"\n    {info['artist']} — {info['title']}")
            print(f"      {info['tracks']} track(s) credit: "
                  f"{', '.join(info['guests'])}")
            apply_album(s["key"], rk, info,
                        do_title=not args.no_title,
                        do_collection=not args.no_collection,
                        apply=args.apply)
    if args.playlists and tracks_by_guest:
        print(f"\n  Cross-library playlists ({len(tracks_by_guest)} guest(s)):")
        do_playlists(tracks_by_guest, args.apply, args.min_tracks)

    if not total:
        print("\n  Nothing to do — no guest credits found.")
    elif not args.apply:
        print(f"\n  DRY RUN over {total} album(s). Add --apply to make changes.")
    else:
        print(f"\n  Updated {total} album(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
