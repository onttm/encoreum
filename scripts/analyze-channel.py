#!/usr/bin/env python3
"""
analyze-channel.py
=========================================================================
Work out how a YouTube channel names things, BEFORE you download from it.

WHY THIS EXISTS
    Every concert channel encodes date/venue/artist differently, and there is
    no single parse that works across them. Surveying five channels by hand
    produced five different extraction strategies:

        @bandcconcerts   date+venue on description line 1, full setlist chapters
        @bandafan           date in the title, year precision only
        @HDPinkFloyd        date in a title parenthetical, no chapters, TWO artists
        @frontrowmusic      multi-artist, documentaries mixed in, release names
        @MontrealRocks      prose descriptions, venue names are the only signal

    Hard-coding a rule table per channel does not scale and goes stale. This
    script infers the convention instead: it reads a channel, measures which
    extraction strategy actually works and how often, and prints a config you
    can act on — or feeds it to the caller as JSON.

WHAT IT MEASURES
    * Where dates live (title vs description) and at what precision, with a HIT
      RATE for each candidate strategy, so you pick on evidence not vibes.
    * Recurring title shapes — the actual naming templates, with counts.
    * Artist cardinality: single-artist channel or multi-artist compilation feed.
    * Chapter coverage (chapters are per-song setlists on some channels).
    * Content mix: concert / single / compilation / documentary / interview.
    * Quality tiers, since some channels post both remasters and old rips.

COST MODEL — this is why it is fast
    Titles are nearly free in bulk (`--flat-playlist` fetches the whole
    catalogue in one call), so titles are analysed for EVERY video.
    Descriptions, chapters and formats need a per-video request, so those are
    sampled. This matters: a 12-video sample of @MontrealRocks gave the WRONG
    answer ("skip this channel"); a full-catalogue title scan found the
    concerts. Sample titles deeply, sample metadata shallowly.

USAGE
    ./analyze-channel.py https://www.youtube.com/@bandcconcerts/videos
    ./analyze-channel.py <url> --sample 40 --titles 500
    ./analyze-channel.py <url> --json            # machine-readable
    ./analyze-channel.py <url> --show-titles     # print titles per bucket

Standalone: Python 3 stdlib + the yt-dlp binary. No Claude, no API key.
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
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

# --------------------------------------------------------------------------
# yt-dlp discovery: prefer the app's self-updating venv copy, fall back to PATH.
# --------------------------------------------------------------------------
def find_ytdlp() -> str:
    for cand in (os.environ.get("YTDLP_BIN"),
                 "/config/venv/bin/yt-dlp",
                 shutil.which("yt-dlp")):
        if cand and os.path.exists(cand):
            return cand
    sys.exit("yt-dlp not found. Set YTDLP_BIN or install yt-dlp.")


MONTHS = ("january february march april may june july august september "
          "october november december").split()
MON_RE = "|".join(m[:3] for m in MONTHS)

# Ordered date strategies. Each yields (precision, iso-ish string).
# Precision matters: a year-only hit cannot build "YYYY.MM.DD - venue" names.
DATE_PATTERNS = [
    ("iso_bracket", re.compile(r"[\[\(](\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})[\]\)]"), "full"),
    ("iso_bare",    re.compile(r"\b(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})\b"), "full"),
    ("d_month_y",   re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({MON_RE})[a-z]*\.?,?\s+(\d{{4}})\b", re.I), "full"),
    ("month_d_y",   re.compile(rf"\b({MON_RE})[a-z]*\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I), "full"),
    ("month_y",     re.compile(rf"\b({MON_RE})[a-z]*\.?\s+(\d{{4}})\b", re.I), "month"),
    ("year_paren",  re.compile(r"[\[\(](\d{4})[\]\)]"), "year"),
    ("year_bare",   re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b"), "year"),
]

# Content classification. ORDER IS LOAD-BEARING: negatives run first, because
# "HANORAH on playing live with friends" contains "live" and is an interview.
NEGATIVE = re.compile(
    r"\b(interview|interviews|talks?\b|chats?\b|reveals?|sits down|q ?& ?a|"
    r"on playing|on writing|on being|reacts?|explains?|discusses)", re.I)
DOCUMENTARY = re.compile(
    r"\b(documentary|full doc\b|the story of|classic albums|biography|"
    r"the making of|behind the scenes)", re.I)
COMPILATION = re.compile(
    r"\b(on repeat|compilation|best of|greatest hits|time capsule|beginnings|"
    r"tv appearances|demos?\b|rehearsals?|box set|anthology)", re.I)
HIGHLIGHTS = re.compile(r"\b(highlights?|tour highlights)\b", re.I)
CONCERT = re.compile(
    r"\b(full concert|full show|live at|live in|live from|in concert|"
    r"concert|\(incomplete\)|full set|live \d{4})\b", re.I)

# A venue gazetteer is the single strongest signal on city-focused channels —
# it is what catches titles containing neither "live" nor "concert".
VENUE_HINTS = re.compile(
    r"\b(arena|stadium|centre|center|theatre|theater|hall|amphitheat\w+|"
    r"coliseum|auditorium|ballroom|club|pavilion|forum|garden|dome|odeon|"
    r"bowl|palace|academy|astoria|olympia|fillmore|red rocks)\b", re.I)


def run_ytdlp(args: list[str], timeout: int = 300) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ""
    return r.stdout or ""


def fetch_titles(ytdlp: str, url: str, limit: int) -> list[dict]:
    """Cheap: whole catalogue in one call, titles + ids only."""
    out = run_ytdlp([ytdlp, "-J", "--flat-playlist", "--no-warnings",
                     "--playlist-end", str(limit), url], timeout=600)
    if not out.strip():
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    entries = data.get("entries") or []
    res = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        # Channel URLs can nest tabs -> entries of entries.
        if e.get("_type") == "playlist" and e.get("entries"):
            for sub in e["entries"]:
                if isinstance(sub, dict) and sub.get("title"):
                    res.append({"id": sub.get("id"), "title": sub["title"],
                                "duration": sub.get("duration")})
            continue
        if e.get("title"):
            res.append({"id": e.get("id"), "title": e["title"],
                        "duration": e.get("duration")})
    return res


def fetch_one(ytdlp: str, vid: str) -> dict | None:
    out = run_ytdlp([ytdlp, "-J", "--no-warnings", "--skip-download",
                     f"https://www.youtube.com/watch?v={vid}"], timeout=120)
    if not out.strip():
        return None
    try:
        d = json.loads(out)
    except json.JSONDecodeError:
        return None
    return {
        "id": d.get("id"),
        "title": d.get("title") or "",
        "description": d.get("description") or "",
        "duration": d.get("duration") or 0,
        "chapters": len(d.get("chapters") or []),
        "live_status": d.get("live_status"),
        "height": (d.get("height")
                   or max([f.get("height") or 0 for f in (d.get("formats") or [])] or [0])),
        "fps": d.get("fps"),
        "view_count": d.get("view_count"),
        "uploader": d.get("uploader") or d.get("channel") or "",
    }


def find_date(text: str) -> tuple[str, str] | None:
    """Return (pattern_name, precision) for the first strategy that hits."""
    if not text:
        return None
    for name, rx, precision in DATE_PATTERNS:
        if rx.search(text):
            return name, precision
    return None


def classify(title: str, duration: int | None, chapters: int) -> str:
    """Bucket a video. Negatives first — see NEGATIVE above for why."""
    if NEGATIVE.search(title):
        return "interview"
    if DOCUMENTARY.search(title):
        return "documentary"
    if COMPILATION.search(title):
        return "compilation"
    if HIGHLIGHTS.search(title):
        return "highlights"
    if CONCERT.search(title) or VENUE_HINTS.search(title):
        return "concert"
    # Tiebreakers only. Duration alone misfires in BOTH directions: a 31-minute
    # Band C set is a real concert, a 29-minute interview is not music at all.
    if duration:
        if chapters >= 5 and duration > 1800:
            return "concert"
        if duration < 600:
            return "single"
        if duration > 3600:
            return "compilation"
    return "other"


def artist_prefix(title: str) -> str | None:
    """Text before the first ' - ' or ': ' — the usual artist slot."""
    m = re.split(r"\s+[-–—:]\s+", title, maxsplit=1)
    if len(m) == 2 and 1 <= len(m[0]) <= 40:
        return m[0].strip()
    return None


def title_shape(title: str, artists: set[str]) -> str:
    """Collapse a title to its structural template so shapes can be counted."""
    s = title
    for name, rx, _ in DATE_PATTERNS:
        s = rx.sub("{DATE}" if name.startswith(("iso", "d_month", "month_d")) else "{YEAR}", s)
    for a in sorted(artists, key=len, reverse=True):
        if a and s.startswith(a):
            s = "{ARTIST}" + s[len(a):]
            break
    s = VENUE_HINTS.sub("{VENUE}", s)
    # Collapse remaining ALLCAPS or Title-Case runs of 1-4 words into {PLACE}.
    s = re.sub(r"\b(?:[A-Z][A-Za-z']+[, ]{0,2}){1,4}\b",
               lambda m: m.group(0) if m.group(0).strip() in ("{ARTIST}", "{VENUE}") else "{PLACE}", s)
    s = re.sub(r"\{PLACE\}(\s*\{PLACE\})+", "{PLACE}", s)
    return re.sub(r"\s+", " ", s).strip()


def pct(n: int, total: int) -> str:
    return f"{(100.0 * n / total):.0f}%" if total else "—"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", help="channel, playlist or /videos URL")
    ap.add_argument("--titles", type=int, default=300,
                    help="how many titles to scan (cheap; default 300)")
    ap.add_argument("--sample", type=int, default=25,
                    help="how many videos to fetch full metadata for (default 25)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--json", action="store_true", help="emit JSON only")
    ap.add_argument("--show-titles", action="store_true",
                    help="list titles under each content bucket")
    args = ap.parse_args()

    ytdlp = find_ytdlp()
    if not args.json:
        print(f"Reading catalogue (up to {args.titles} titles)…", file=sys.stderr)
    items = fetch_titles(ytdlp, args.url, args.titles)
    if not items:
        sys.exit("No videos found. Check the URL (try the /videos tab).")

    titles = [i["title"] for i in items]
    n = len(titles)

    # ---- artist cardinality (titles only) --------------------------------
    prefixes = Counter(p for p in (artist_prefix(t) for t in titles) if p)
    artists = {a for a, c in prefixes.items() if c >= max(2, n * 0.05)}
    multi_artist = len(artists) > 1

    # ---- date strategy hit rates (titles only) ---------------------------
    title_hits: Counter = Counter()
    title_prec: Counter = Counter()
    for t in titles:
        got = find_date(t)
        if got:
            title_hits[got[0]] += 1
            title_prec[got[1]] += 1

    # ---- deeper sample ---------------------------------------------------
    ids = [i["id"] for i in items if i.get("id")]
    step = max(1, len(ids) // max(1, args.sample))
    sample_ids = ids[::step][:args.sample]
    if not args.json:
        print(f"Sampling {len(sample_ids)} videos for description/chapters…",
              file=sys.stderr)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        meta = [m for m in ex.map(lambda v: fetch_one(ytdlp, v), sample_ids) if m]

    desc_hits: Counter = Counter()
    desc1_hits: Counter = Counter()
    chapters_present = 0
    chapter_counts = []
    live_states: Counter = Counter()
    heights: Counter = Counter()
    venue_in_desc = 0
    for m in meta:
        d = m["description"]
        line1 = d.splitlines()[0] if d else ""
        g1 = find_date(line1)
        if g1:
            desc1_hits[g1[0]] += 1
        gd = find_date(d[:600])
        if gd:
            desc_hits[gd[0]] += 1
        if VENUE_HINTS.search(line1 or d[:300]):
            venue_in_desc += 1
        if m["chapters"]:
            chapters_present += 1
            chapter_counts.append(m["chapters"])
        live_states[m.get("live_status") or "unknown"] += 1
        if m.get("height"):
            heights[m["height"]] += 1

    # ---- content mix (titles for breadth, sample durations where known) ---
    dur_by_id = {i["id"]: i.get("duration") for i in items}
    chap_by_id = {m["id"]: m["chapters"] for m in meta}
    buckets: defaultdict[str, list] = defaultdict(list)
    for i in items:
        b = classify(i["title"], dur_by_id.get(i["id"]), chap_by_id.get(i["id"], 0))
        buckets[b].append(i["title"])

    # ---- title shapes ----------------------------------------------------
    shapes = Counter(title_shape(t, artists) for t in titles)

    # ---- recommendation --------------------------------------------------
    ns = len(meta) or 1
    cands = [
        ("description line 1", desc1_hits.most_common(1)[0][1] if desc1_hits else 0, ns),
        ("description (first 600 chars)", desc_hits.most_common(1)[0][1] if desc_hits else 0, ns),
        ("title", sum(title_hits.values()), n),
    ]
    cands.sort(key=lambda c: (c[1] / c[2]) if c[2] else 0, reverse=True)
    best_field, best_hit, best_tot = cands[0]
    best_precision = title_prec.most_common(1)[0][0] if title_prec else "unknown"

    result = {
        "url": args.url,
        "titles_scanned": n,
        "metadata_sampled": len(meta),
        "artists": sorted(artists)[:10],
        "multi_artist": multi_artist,
        "date_strategy": {
            "recommended_field": best_field,
            "hit_rate": round(best_hit / best_tot, 3) if best_tot else 0,
            "precision": best_precision,
            "title_patterns": title_hits.most_common(),
            "desc_line1_patterns": desc1_hits.most_common(),
        },
        "venue_signal_rate": round(venue_in_desc / ns, 3),
        "chapters": {
            "coverage": round(chapters_present / ns, 3),
            "median": sorted(chapter_counts)[len(chapter_counts) // 2] if chapter_counts else 0,
        },
        "live_status": dict(live_states),
        "resolutions": dict(heights.most_common(5)),
        "content_mix": {k: len(v) for k, v in sorted(buckets.items(),
                                                     key=lambda x: -len(x[1]))},
        "title_shapes": shapes.most_common(6),
    }

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    # ---------------- human report ----------------
    W = 78
    print()
    print("=" * W)
    print(f"CHANNEL ANALYSIS  ·  {args.url}")
    print(f"{n} titles scanned · {len(meta)} sampled for metadata")
    print("=" * W)

    print("\nARTIST")
    if multi_artist:
        print(f"  MULTI-ARTIST ({len(artists)} found) — the Plex show CANNOT be")
        print("  taken from the channel; parse it from the title prefix.")
        for a, c in prefixes.most_common(6):
            print(f"     {c:>4}  {a}")
    elif artists:
        a = next(iter(artists))
        print(f"  single artist: {a} ({prefixes[a]}/{n} titles)")
    else:
        print("  could not determine — titles have no consistent prefix")

    print("\nDATE EXTRACTION — pick the highest hit rate")
    for field, hit, tot in cands:
        bar = "#" * int(20 * hit / tot) if tot else ""
        print(f"  {field:<32} {pct(hit, tot):>5}  {bar}")
    print(f"  -> recommended: {best_field}  (precision: {best_precision})")
    if best_precision == "year":
        print("     WARNING: year-only precision cannot build YYYY.MM.DD names.")
    if title_hits:
        print("  title patterns seen: " +
              ", ".join(f"{k}({v})" for k, v in title_hits.most_common(4)))
    if desc1_hits:
        print("  desc-line-1 patterns: " +
              ", ".join(f"{k}({v})" for k, v in desc1_hits.most_common(4)))

    print("\nCHAPTERS")
    cov = chapters_present / ns
    print(f"  {pct(chapters_present, ns)} of sampled videos have chapters"
          + (f", median {result['chapters']['median']} per video" if chapter_counts else ""))
    if cov > 0.6 and result["chapters"]["median"] >= 5:
        print("  -> likely per-song setlists: embedded chapters give seek points,")
        print("     and --split-chapters could produce per-song Singles.")
    elif cov < 0.2:
        print("  -> no setlist data here; chapter-based features will not work.")

    print("\nCONTENT MIX")
    for k, v in sorted(buckets.items(), key=lambda x: -len(x[1])):
        print(f"  {k:<12} {len(v):>4}  {pct(len(v), n):>5}")
        if args.show_titles:
            for t in v[:5]:
                print(f"                  · {t[:60]}")
    concerts = len(buckets.get("concert", []))
    junk = len(buckets.get("interview", [])) + len(buckets.get("documentary", []))
    if concerts == 0:
        print("  -> NOT a concert source. Do not add this channel.")
    elif junk > concerts:
        print("  -> mostly non-concert content; filter hard or whitelist videos.")

    if heights:
        print("\nQUALITY TIERS")
        for h, c in heights.most_common(4):
            print(f"  {h}p{'':<6} {c:>4} of {ns} sampled")
        if len(heights) > 2:
            print("  -> mixed tiers on one channel; prefer the better uploads.")

    if live_states:
        print("\nLIVE STATUS")
        print("  " + ", ".join(f"{k}={v}" for k, v in live_states.most_common()))

    print("\nTITLE SHAPES (inferred templates)")
    for shape, c in shapes.most_common(6):
        print(f"  {c:>4}  {shape[:66]}")

    print("\n" + "-" * W)
    print("SUGGESTED EXTRACTION CONFIG")
    print("-" * W)
    print(json.dumps({
        "date_from": best_field,
        "date_precision": best_precision,
        "artist_from": "title_prefix" if multi_artist else "channel",
        "use_chapters": cov > 0.6,
        "expect_buckets": [k for k in buckets if buckets[k]],
        "usable": concerts > 0,
    }, indent=2))
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
