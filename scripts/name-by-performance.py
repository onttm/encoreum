#!/usr/bin/env python3
"""
name-by-performance.py
=========================================================================
Rename downloaded concert videos to *performance-descriptor* filenames,
organised in bare per-year folders (no "Season" word anywhere):

        <Show>/<YYYY>/YYYY.MM.DD - tour - venue, city, state, country - NNN.ext
        e.g.  Band A/2020/2020.04.24 - X72 - Foro Sol, Mexico City, Mexico - 001.mkv

Every field is optional. Whatever we can't determine is dropped **along with
its separator**, so a video with only a date collapses to:

        <Show>/2020/2020.04.24 - 001.mkv

and one with a date + tour but no venue to:

        <Show>/2020/2020.04.24 - X72 - 001.mkv

NNN is a **3-digit** sequence that disambiguates videos sharing the same
descriptor (e.g. every song from one night in a per-song playlist). It is
numbered within each identical-name group, in concert-date / playlist order,
so the files stay unique on disk and Plex keeps them as separate episodes
(generate-nfo.py assigns each a unique episode number within its year).

WHERE THE FIELDS COME FROM
    date      the concert (footage) date, parsed from the video TITLE, then
              DESCRIPTION, then the release/upload date  (identical logic to
              rename-by-footage-date.py, which this reuses).
    tour      --tour if you pass it, else the playlist title (when it looks
              like a real name and not "Uploads from …").
    venue,    parsed from the title/description ("Live at <venue>, <city>,
    city,     <state>, <country>"). This metadata is usually NOT in YouTube's
    state,    data, so most files gracefully collapse to date (+ tour). That is
    country   expected — better a clean short name than a wrong guess.

It reads only .info.json and renames videos + their sidecars; it never
touches file contents.

USAGE
    python3 name-by-performance.py "/path/to/Concerts/Band A" --show "Band A" [--tour "X72"] [--apply]

Note: numbering is derived from the whole current file set. Adding more videos
later can shift sequence numbers within a group, so download a night/tour in
one go where possible.
=========================================================================
"""
import argparse
import importlib.util
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# Reuse find_date / date_from_meta / clean / move_with_sidecars / VIDEO_EXTS / ILLEGAL
# from rename-by-footage-date.py so dating + sidecar handling never drift.
_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "cdl_rename_footage", str(_HERE / "rename-by-footage-date.py"))
_R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_R)


# --- location parsing ------------------------------------------------------
# Conservative on purpose: we would rather omit a field than invent a wrong one.
_COUNTRIES = {
    "usa": "USA", "us": "USA", "u.s.": "USA", "u.s.a.": "USA",
    "united states": "USA", "united states of america": "USA", "america": "USA",
    "uk": "UK", "u.k.": "UK", "united kingdom": "UK", "great britain": "UK",
    "england": "England", "scotland": "Scotland", "wales": "Wales",
    "ireland": "Ireland", "northern ireland": "Northern Ireland",
    "canada": "Canada", "mexico": "Mexico", "brazil": "Brazil", "brasil": "Brazil",
    "argentina": "Argentina", "chile": "Chile", "colombia": "Colombia",
    "peru": "Peru", "germany": "Germany", "france": "France", "spain": "Spain",
    "portugal": "Portugal", "italy": "Italy", "netherlands": "Netherlands",
    "belgium": "Belgium", "austria": "Austria", "switzerland": "Switzerland",
    "sweden": "Sweden", "norway": "Norway", "denmark": "Denmark",
    "finland": "Finland", "poland": "Poland", "czechia": "Czechia",
    "czech republic": "Czech Republic", "hungary": "Hungary", "greece": "Greece",
    "russia": "Russia", "japan": "Japan", "china": "China", "korea": "Korea",
    "south korea": "South Korea", "australia": "Australia",
    "new zealand": "New Zealand", "india": "India", "south africa": "South Africa",
    "turkey": "Turkey", "israel": "Israel", "iceland": "Iceland",
    "croatia": "Croatia", "serbia": "Serbia", "romania": "Romania",
    "slovenia": "Slovenia", "slovakia": "Slovakia", "bulgaria": "Bulgaria",
    "luxembourg": "Luxembourg", "estonia": "Estonia", "latvia": "Latvia",
    "lithuania": "Lithuania",
}
_US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}
_STATE_ABBRS = set(_US_STATES.values())
_VENUE_WORDS = (
    "arena", "stadium", "theatre", "theater", "hall", "center", "centre",
    "club", "amphitheater", "amphitheatre", "forum", "foro", "field", "park",
    "gardens", "garden", "dome", "coliseum", "colosseum", "pavilion",
    "auditorium", "ballroom", "bowl", "palladium", "academy", "festival",
    "grounds", "raceway", "speedway", "wembley", "o2", "arena's",
)
# stop the location phrase at a closing bracket, a pipe, or a spaced dash/colon
_LOC_RE = re.compile(
    r"\blive\s+(?:at|from|in)\s+(.+?)(?=[)\]|]|\s[-–—:]\s|$)", re.I)
# reject date-ish segments, but KEEP venue names with incidental digits ("O2 Arena")
_MONTH_ANY = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b", re.I)
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")


def _looks_like_place(seg: str) -> bool:
    """Reject obvious non-places (dates, years, numbers, empties, junk)."""
    s = seg.strip()
    if not s or len(s) > 60:
        return False
    if s.isdigit():                   # "2019", "11"
        return False
    if _MONTH_ANY.search(s):          # "September 8", "NY on July 11"
        return False
    if _YEAR.search(s):               # "England on 12 May 2023"
        return False
    low = s.lower()
    if low in ("live", "official", "full concert", "full show", "hd", "4k"):
        return False
    return True


def parse_location(text: str) -> dict:
    """Best-effort {venue, city, state, country}; any/all may be None."""
    out = {"venue": None, "city": None, "state": None, "country": None}
    if not text:
        return out
    m = _LOC_RE.search(text)
    if not m:
        return out
    segs = [s.strip(" .") for s in m.group(1).split(",")]
    segs = [s for s in segs if _looks_like_place(s)]
    if not segs:
        return out

    # country (from the tail)
    if segs and segs[-1].lower() in _COUNTRIES:
        out["country"] = _COUNTRIES[segs[-1].lower()]
        segs.pop()
    # state (only meaningful for US-style tails, but harmless elsewhere)
    if segs:
        tail = segs[-1]
        if tail.lower() in _US_STATES:
            out["state"] = _US_STATES[tail.lower()]
            segs.pop()
        elif tail.upper() in _STATE_ABBRS and len(tail) == 2:
            out["state"] = tail.upper()
            segs.pop()

    def _is_venue(s: str) -> bool:
        low = f" {s.lower()} "
        return any(f" {w} " in low or s.lower().startswith(w) or s.lower().endswith(w)
                   for w in _VENUE_WORDS)

    if len(segs) >= 2:
        # first venue-ish segment is the venue, the next non-venue is the city
        vi = next((i for i, s in enumerate(segs) if _is_venue(s)), None)
        if vi is not None:
            out["venue"] = segs[vi]
            rest = [s for i, s in enumerate(segs) if i != vi]
            out["city"] = rest[0] if rest else None
        else:
            out["venue"], out["city"] = segs[0], segs[1]
    elif len(segs) == 1:
        if _is_venue(segs[0]):
            out["venue"] = segs[0]
        else:
            out["city"] = segs[0]
    return out


# --- tour ------------------------------------------------------------------
_TOUR_JUNK = re.compile(r"uploads?\s+from|^\s*favorites|- topic$|- playlist$", re.I)


def extract_tour(info: dict, override: str | None) -> str | None:
    if override and override.strip():
        return override.strip()
    for key in ("playlist_title", "playlist"):
        t = (info.get(key) or "").strip()
        if not t or _TOUR_JUNK.search(t):
            continue
        # drop a leading "<Artist> - " if present; keep the descriptive tail
        return t
    return None


# --- name assembly ---------------------------------------------------------
def build_core(date, tour: str | None, loc: dict) -> str:
    """Assemble 'YYYY.MM.DD - tour - venue, city, state, country', omitting any
    missing part together with its separator."""
    fields = []
    if date:
        y, mo, d = date
        fields.append(f"{y:04d}.{mo:02d}.{d:02d}")
    if tour:
        fields.append(tour)
    geo = [loc.get("venue"), loc.get("city"), loc.get("state"), loc.get("country")]
    geo = [g for g in geo if g]
    if geo:
        fields.append(", ".join(geo))
    core = " - ".join(fields)
    # sanitise (illegal FS chars -> space, collapse, trim) without harming ", " / " - "
    return _R.clean(core) if core else ""


def _load_info(video: Path) -> dict:
    p = video.parent / (video.name[: -len(video.suffix)] + ".info.json")
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def main():
    ap = argparse.ArgumentParser(
        description="Rename concerts to 'YYYY.MM.DD - tour - venue, city, state, country - NNN' in bare year folders.")
    ap.add_argument("folder", help="The show folder (may still contain old Season subfolders).")
    ap.add_argument("--show", required=True, help='Series name, e.g. "Band A".')
    ap.add_argument("--tour", default=None, help="Tour name to use for every video (optional).")
    ap.add_argument("--apply", action="store_true", help="Actually rename (default is a dry-run preview).")
    args = ap.parse_args()

    root = Path(args.folder).expanduser()
    if not root.is_dir():
        sys.exit(f"Not a folder: {root}")
    videos = [p for p in root.rglob("*") if p.suffix.lower() in _R.MEDIA_EXTS]
    if not videos:
        sys.exit(f"No video files found under {root}")

    print(f"{'APPLYING' if args.apply else 'PREVIEW (no changes)'} - {len(videos)} videos\n")

    # 1) resolve each video's (date, core-name, sort-key)
    dated, undated = [], []
    for v in sorted(videos):
        info = _load_info(v)
        title = info.get("title") or v.stem
        desc = info.get("description") or ""
        date = _R.find_date(title) or _R.find_date(desc) or _R.date_from_meta(info)
        tour = extract_tour(info, args.tour)
        loc = parse_location(title)
        if not any(loc.values()):
            loc = parse_location(desc)
        core = build_core(date, tour, loc)
        pidx = info.get("playlist_index")
        sortkey = (date or (9999, 99, 99), int(pidx) if str(pidx).isdigit() else 0, v.name)
        if date:
            dated.append((date, core, v, sortkey))
        else:
            undated.append((core, v, sortkey))

    # 2) group by (year, core) and assign a 3-digit within-group sequence
    groups = defaultdict(list)
    for date, core, v, sk in dated:
        groups[(date[0], core)].append((sk, v, core))
    for core, v, sk in undated:
        groups[("undated", core)].append((sk, v, core))

    plan = []
    for (year, core), members in groups.items():
        members.sort(key=lambda e: e[0])
        folder = root / (str(year) if year != "undated" else "undated")
        for i, (sk, v, core_name) in enumerate(members, start=1):
            base = f"{core_name} - {i:03d}" if core_name else f"{i:03d}"
            plan.append((v, folder / f"{base}{v.suffix}"))

    plan.sort(key=lambda pr: str(pr[1]))
    for v, new_path in plan:
        print(f"{v.relative_to(root)}\n   -> {new_path.relative_to(root)}")

    if args.apply:
        moved = 0
        for v, new_path in plan:
            if v.resolve() == new_path.resolve():
                continue
            moved += _R.move_with_sidecars(v, new_path)
        # tidy up any now-empty old "Season *" folders left behind by the moves
        for d in sorted(root.glob("Season *"), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        print(f"\nDone. Renamed {len(plan)} videos ({moved} files moved).")
    else:
        print(f"\nDry run only. Re-run with --apply to make these {len(plan)} changes.")


if __name__ == "__main__":
    main()
