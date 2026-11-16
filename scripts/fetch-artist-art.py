#!/usr/bin/env python3
"""
fetch-artist-art.py -- artist posters for a library with no metadata agent.

WHY THIS IS NEEDED AT ALL
    The Live Music library runs on agent `com.plexapp.agents.none`, and that is
    the correct choice: the scanner still reads embedded tags, so albums group
    and per-track credits survive, but nothing tries to "correct" a 1995
    bootleg into a studio release it is not.

    The cost of that choice is that Plex fetches no artist images either. So
    they have to be local files, and this fetches them.

THE CHAIN, AND WHY IT NEEDS NO API KEY
        artist name
          -> MusicBrainz          the artist's MBID          (published 1 req/s)
          -> MusicBrainz url-rels the Wikidata QID
          -> Wikidata P18/P154    the Commons filename
          -> Wikimedia Commons    the actual image

    Every step is a free, anonymous, documented endpoint. No account, no key,
    nothing to expire, nothing to bill. That matters more than convenience here:
    a shipped tool must keep working for whoever inherits it, and a key they
    cannot renew is a feature that dies quietly.

    Verified end to end on Band B: MBID b7ffd2af…, Wikidata Q11647,
    "Band B Munich 2007.jpg", 253 KB, HTTP 200.

LICENCE IS RECORDED, NOT ASSUMED
    Commons images are freely licensed but most are CC-BY-SA, which requires
    ATTRIBUTION — the licence is free, not public domain. So the author, licence
    and source URL are written next to every image in `artwork-credits.txt`.

    Same reasoning as the setlist.fm attribution: a credit that lives only in a
    database or a UI footer does not survive the file being copied, the app
    being rebuilt, or the library being handed to someone else. Put it beside
    the artifact.

USAGE
    ./scripts/fetch-artist-art.py --scan                    # what is missing
    ./scripts/fetch-artist-art.py "Band B"         # preview one
    ./scripts/fetch-artist-art.py --scan --apply            # fetch them all
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

LIVE_AUDIO_DIR = Path(os.environ.get("LIVE_AUDIO_DIR", "/library/live-music"))
FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")

# A descriptive User-Agent with contact info is REQUIRED by MusicBrainz and
# expected by Wikimedia. A generic one gets throttled or blocked, and rightly:
# it makes an anonymous client uncontactable when it misbehaves.
CONTACT = os.environ.get("ART_CONTACT", "encoreum")
UA = f"encoreum/1.0 ( {CONTACT} )"

MB = "https://musicbrainz.org/ws/2"
WD = "https://www.wikidata.org/wiki/Special:EntityData"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
COMMONS_FILE = "https://commons.wikimedia.org/wiki/Special:FilePath/"

# MusicBrainz publishes 1 request/second and enforces it with HTTP 503 — met
# during this work by going faster. 1.2s leaves margin for clock jitter.
MB_INTERVAL = 1.2
_last_mb = 0.0


def _get(url: str, accept_json: bool = True, timeout: int = 30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r) if accept_json else r.read()


class Unavailable(Exception):
    """MusicBrainz could not be reached or refused the request.

    A distinct type on purpose. Returning None for BOTH "no such artist" and
    "rate-limited" makes a transient failure read as a definitive negative —
    "no exact MusicBrainz match" for a band that is plainly there. That is
    exactly what happened on the first run of this script: a 503 from going too
    fast earlier was reported to the user as the artist not existing.
    """


def _mb(path: str, tries: int = 3) -> dict:
    """Rate-limited MusicBrainz call. Raises Unavailable rather than lying."""
    global _last_mb
    last = ""
    for attempt in range(tries):
        wait = MB_INTERVAL - (time.time() - _last_mb)
        if wait > 0:
            time.sleep(wait)
        _last_mb = time.time()
        try:
            return _get(f"{MB}{path}")
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code == 503:
                # Their documented throttle response. Backing off and retrying
                # is the expected client behaviour, not an error to surface.
                time.sleep(3 * (attempt + 1))
                continue
            raise Unavailable(last) from e
        except Exception as e:  # noqa: BLE001
            last = type(e).__name__
            time.sleep(2 * (attempt + 1))
    raise Unavailable(last or "unreachable")


def resolve_mbid(artist: str) -> tuple[str | None, str | None]:
    """Artist name -> (mbid, display name). Exact matches only.

    Anything less than an exact name match is refused rather than guessed: this
    writes a picture of a PERSON into a folder, and the failure mode of a fuzzy
    match is a photo of the wrong band sitting there looking authoritative.
    """
    q = urllib.parse.quote(f'artist:"{artist}"')
    d = _mb(f"/artist?query={q}&fmt=json&limit=5")
    if not d.get("artists"):
        return None, None
    want = artist.strip().lower()
    for a in d["artists"]:
        if (a.get("name") or "").strip().lower() == want:
            return a["id"], a["name"]
    return None, None


def wikidata_qid(mbid: str) -> str | None:
    d = _mb(f"/artist/{mbid}?inc=url-rels&fmt=json")
    for r in d.get("relations", []):
        if r.get("type") == "wikidata":
            return r["url"]["resource"].rstrip("/").split("/")[-1]
    return None


def commons_filename(qid: str) -> tuple[str | None, str | None]:
    """Wikidata P18 (image), falling back to P154 (logo)."""
    try:
        d = _get(f"{WD}/{qid}.json")
    except Exception:  # noqa: BLE001
        return None, None
    claims = (d.get("entities", {}).get(qid, {}) or {}).get("claims", {})
    for prop, kind in (("P18", "image"), ("P154", "logo")):
        if prop in claims:
            try:
                return claims[prop][0]["mainsnak"]["datavalue"]["value"], kind
            except (KeyError, IndexError, TypeError):
                continue
    return None, None


def commons_credit(filename: str) -> dict:
    """Author + licence for a Commons file. Attribution is a condition of use."""
    q = urllib.parse.urlencode({
        "action": "query", "format": "json", "prop": "imageinfo",
        "iiprop": "extmetadata|url|size", "titles": f"File:{filename}"})
    try:
        d = _get(f"{COMMONS_API}?{q}")
    except Exception:  # noqa: BLE001
        return {}
    pages = (d.get("query", {}) or {}).get("pages", {})
    for p in pages.values():
        ii = (p.get("imageinfo") or [{}])[0]
        ex = ii.get("extmetadata", {}) or {}

        def val(k):
            v = (ex.get(k) or {}).get("value")
            if not v:
                return ""
            import re as _re
            return _re.sub(r"<[^>]+>", "", str(v)).strip()

        return {"author": val("Artist"), "licence": val("LicenseShortName"),
                "credit": val("Credit"), "url": ii.get("descriptionurl", ""),
                "width": ii.get("width"), "height": ii.get("height")}
    return {}


def commons_url(filename: str, width: int = 0) -> str:
    u = COMMONS_FILE + urllib.parse.quote(filename.replace(" ", "_"))
    # Special:FilePath honours a width parameter, so we ask Wikimedia to do the
    # resizing rather than downloading a 20 MB original to shrink it locally.
    return f"{u}?width={width}" if width else u


def download(url: str, dest: Path) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=90) as r:
            data = r.read()
        if len(data) < 2000:            # a stub or an error page, not a photo
            return False
        dest.write_bytes(data)
        return True
    except Exception:  # noqa: BLE001
        return False


def resolve(artist: str) -> dict:
    """artist name -> everything needed to fetch and credit an image."""
    out = {"artist": artist, "mbid": None, "qid": None, "file": None,
           "kind": None, "credit": {}, "error": None}
    try:
        mbid, name = resolve_mbid(artist)
    except Unavailable as e:
        # NOT the same as "not found", and must not be reported as such.
        out["error"] = f"MusicBrainz unavailable ({e}) — try again later"
        out["transient"] = True
        return out
    if not mbid:
        out["error"] = "no exact MusicBrainz match — refusing to guess"
        return out
    out["mbid"], out["artist"] = mbid, name
    try:
        qid = wikidata_qid(mbid)
    except Unavailable as e:
        out["error"] = f"MusicBrainz unavailable ({e}) — try again later"
        out["transient"] = True
        return out
    if not qid:
        out["error"] = "MusicBrainz has no Wikidata link for this artist"
        return out
    out["qid"] = qid
    fn, kind = commons_filename(qid)
    if not fn:
        out["error"] = "Wikidata has no image (P18) or logo (P154)"
        return out
    out["file"], out["kind"] = fn, kind
    out["credit"] = commons_credit(fn)
    return out


def write_credits(folder: Path, info: dict) -> None:
    """Attribution beside the image, because most Commons files are CC-BY-SA.

    Freely licensed is not the same as public domain. The credit goes next to
    the artifact for the same reason the setlist.fm credit goes into the NFO: a
    licence obligation recorded only in a database does not survive the file
    being copied, the app being rebuilt, or the library being handed on.
    """
    c = info.get("credit") or {}
    lines = [
        f"artist   : {info['artist']}",
        f"file     : {info['file']}",
        f"author   : {c.get('author') or 'unknown'}",
        f"licence  : {c.get('licence') or 'see source page'}",
        f"source   : {c.get('url') or commons_url(info['file'])}",
        f"via      : MusicBrainz {info['mbid']} -> Wikidata {info['qid']}",
        "",
        "Wikimedia Commons images are freely licensed but most require",
        "attribution. Keep this file with the image.",
    ]
    (folder / "artwork-credits.txt").write_text("\n".join(lines), encoding="utf-8")


def artist_folders(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir()
                  if p.is_dir() and not p.name.startswith("."))


def needs_art(folder: Path) -> bool:
    return not any((folder / n).exists()
                   for n in ("poster.jpg", "artist.jpg", "fanart.jpg"))


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("artists", nargs="*", help="artist name(s); omit with --scan")
    ap.add_argument("--scan", action="store_true",
                    help=f"every artist folder under {LIVE_AUDIO_DIR} missing art")
    ap.add_argument("--library", default=str(LIVE_AUDIO_DIR))
    ap.add_argument("--width", type=int, default=1000,
                    help="ask Wikimedia to resize to this width (0 = original)")
    ap.add_argument("--apply", action="store_true", help="actually download")
    ap.add_argument("--force", action="store_true", help="replace existing art")
    args = ap.parse_args()

    root = Path(args.library)
    if args.scan:
        folders = [f for f in artist_folders(root)
                   if args.force or needs_art(f)]
        names = [f.name for f in folders]
        skipped = len(artist_folders(root)) - len(folders)
        if skipped:
            print(f"  ({skipped} artist(s) already have art; --force to replace)")
    else:
        names = args.artists
        folders = [root / n for n in names]
    if not names:
        print("Nothing to do. Give an artist name or use --scan.")
        return 0

    print(f"  {len(names)} artist(s)\n")
    done = transient = 0
    for name, folder in zip(names, folders):
        info = resolve(name)
        print(f"  {name}")
        if info["error"]:
            print(f"    -> {info['error']}")
            if info.get("transient"):
                transient += 1
            continue
        c = info["credit"]
        print(f"    {info['kind']}: {info['file']}"
              + (f"  ({c.get('width')}x{c.get('height')})" if c.get("width") else ""))
        print(f"    author : {c.get('author') or 'unknown'}")
        print(f"    licence: {c.get('licence') or 'see source page'}")
        if not args.apply:
            print("    (dry run)")
            continue
        if not folder.is_dir():
            print("    -> no such folder in the library; skipped")
            continue
        url = commons_url(info["file"], args.width)
        dest = folder / "poster.jpg"
        if download(url, dest):
            # artist.jpg as well: Plex accepts either, and which one it prefers
            # has changed between versions. Two small files beat a support hunt.
            (folder / "artist.jpg").write_bytes(dest.read_bytes())
            write_credits(folder, info)
            print(f"    -> wrote poster.jpg + artist.jpg "
                  f"({dest.stat().st_size // 1024} KB) + artwork-credits.txt")
            done += 1
        else:
            print("    -> download failed")
    if args.apply:
        print(f"\n  {done}/{len(names)} artist(s) given artwork.")
    else:
        print("\n  DRY RUN. Add --apply to download.")
    if transient:
        print(f"  {transient} artist(s) could not be checked because MusicBrainz "
              "was unavailable — re-run, they are NOT known to be missing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
