#!/usr/bin/env python3
"""
codec-compat.py
=========================================================================
Look up your **playback device's** video-codec hardware support and get a
suggested Encoreum "Convert" preset — so AV1 downloads still play on gear
that can't decode AV1 (Apple TV, older phones, etc.).

Standalone: pure Python stdlib, no Encoreum / no network needed. The data
lives in the editable sibling file **codec-compat.json** — this script reads
it, helps you find your device, and helps you ADD one that's missing (with
links to figure out its support). It also builds the JSON from scratch if the
file is absent, so it doubles as the dataset builder.

USAGE
    python3 codec-compat.py                 # interactive menu
    python3 codec-compat.py lookup apple tv # one-shot lookup
    python3 codec-compat.py list [category] # list all / by category
    python3 codec-compat.py add             # guided add
    python3 codec-compat.py resources       # where to find your device's support

Preset meaning:
    keep  -> device decodes AV1: keep the best-quality/smallest original
    hevc  -> no AV1 but HEVC ok: convert to HEVC/H.265
    h264  -> convert to H.264 for maximum compatibility
=========================================================================
"""
import argparse
import difflib
import json
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parent / "codec-compat.json"

RESOURCES = [
    ("AV1 hardware decode support (Wikipedia)",
     "https://en.wikipedia.org/wiki/AV1#Hardware_encoding_and_decoding"),
    ("HEVC / H.265 hardware support (Wikipedia)",
     "https://en.wikipedia.org/wiki/High_Efficiency_Video_Coding#Hardware"),
    ("AV1 supported devices — running list (Coconut)",
     "https://www.coconut.co/articles/av1-supported-devices-complete-list-updates"),
    ("Intel Quick Sync codec table (Wikipedia)",
     "https://en.wikipedia.org/wiki/Intel_Quick_Sync_Video"),
    ("Nvidia NVDEC decode matrix (Wikipedia)",
     "https://en.wikipedia.org/wiki/Nvidia_NVDEC"),
    ("AMD Video Core Next decode support (Wikipedia)",
     "https://en.wikipedia.org/wiki/Video_Core_Next"),
]


def load() -> dict:
    if DATA.exists():
        return json.loads(DATA.read_text(encoding="utf-8"))
    return {"version": 1, "updated": "", "devices": []}


def save(db: dict) -> None:
    DATA.write_text(json.dumps(db, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def suggest(dev: dict):
    if dev.get("av1"):
        return "keep", "Keep original — your device hardware-decodes AV1 (best quality & size)."
    if dev.get("hevc"):
        return "hevc", "Convert to HEVC/H.265 — no AV1 decode, but HEVC plays and stays efficient."
    return "h264", "Convert to H.264 — maximum compatibility."


def _codecs(dev: dict) -> str:
    y = lambda b: "yes" if b else "no "
    return f"AV1 {y(dev.get('av1'))} · HEVC {y(dev.get('hevc'))} · H.264 {y(dev.get('h264', True))}"


def search(db: dict, query: str) -> list:
    q = query.lower().strip()
    words = q.split()
    scored = []
    for d in db["devices"]:
        terms = [d["name"].lower()] + [a.lower() for a in d.get("aliases", [])]
        hay = " ".join(terms + [d.get("category", "").lower()])
        score = 100 if (q and q in hay) else 0
        score += sum(25 for w in words if w in hay)
        score += int(60 * max([difflib.SequenceMatcher(None, q, t).ratio() for t in terms] + [0]))
        if score >= 30:
            scored.append((score, d))
    scored.sort(key=lambda s: -s[0])
    return [d for _, d in scored[:8]]


def show(dev: dict) -> None:
    code, msg = suggest(dev)
    print(f"\n  {dev['name']}")
    print(f"    {_codecs(dev)}")
    if dev.get("notes"):
        print(f"    note: {dev['notes']}")
    print(f"    -> Suggested preset: [{code}] {msg}")


def print_resources() -> None:
    print("\nCan't find your device? Look up its AV1/HEVC support here, then add it:")
    for name, url in RESOURCES:
        print(f"  * {name}\n      {url}")
    print("  Tip: find the chipset/GPU (e.g. 'Apple A15', 'Snapdragon 8 Gen 2', 'Intel Arc',")
    print("       'RTX 4070') and check whether it HARDWARE-decodes AV1. H.264 is universal.")


def cmd_lookup(db: dict, query: str) -> None:
    if not query:
        print("Give a device or chipset to look up, e.g.  lookup apple tv")
        return
    matches = search(db, query)
    if not matches:
        print(f"\nNo match for '{query}'.")
        print_resources()
        return
    print(f"\nBest matches for '{query}':")
    for d in matches:
        show(d)


def _yn(prompt: str):
    while True:
        v = input(prompt + " [y/n/? = unknown]: ").strip().lower()
        if v in ("y", "yes"):
            return True
        if v in ("n", "no"):
            return False
        if v in ("?", "u", "unknown", ""):
            return None


def cmd_add(db: dict) -> None:
    print("\nAdd a device (blank name cancels).")
    name = input("  Device name (e.g. 'iPhone 17 Pro'): ").strip()
    if not name:
        print("Cancelled.")
        return
    print_resources()
    av1 = _yn("\n  Hardware-decodes AV1?")
    hevc = _yn("  Hardware-decodes HEVC/H.265?")
    aliases = [a.strip() for a in input("  Search aliases (comma-separated, optional): ").split(",") if a.strip()]
    cat = input("  Category (phone/tablet/tv/streamer/console/computer): ").strip() or "other"
    notes = input("  Notes (optional): ").strip()
    if av1 is None:
        notes = (notes + " (AV1 unknown — assumed no)").strip()
    dev = {
        "name": name, "aliases": aliases, "category": cat,
        "av1": bool(av1), "hevc": True if hevc is None else bool(hevc), "h264": True,
        "notes": notes,
    }
    db["devices"].append(dev)
    save(db)
    show(dev)
    print("\nSaved to codec-compat.json. If it's correct, consider sharing it back to the project.")


def cmd_list(db: dict, category=None) -> None:
    devs = [d for d in db["devices"] if not category or d.get("category") == category]
    if not devs:
        print("No devices" + (f" in category '{category}'." if category else "."))
        return
    for d in sorted(devs, key=lambda x: (x.get("category", ""), x["name"])):
        code, _ = suggest(d)
        print(f"  [{code:>4}] {_codecs(d):<34} {d['name']}")


def menu(db: dict) -> None:
    while True:
        print("\nEncoreum — device codec compatibility helper")
        print("  1) Look up my device")
        print("  2) Add a device (not in the list)")
        print("  3) List all devices")
        print("  4) Where to find my device's support")
        print("  5) Quit")
        try:
            c = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if c == "1":
            cmd_lookup(db, input("  Device / chipset: ").strip())
        elif c == "2":
            cmd_add(db)
        elif c == "3":
            cmd_list(db)
        elif c == "4":
            print_resources()
        elif c in ("5", "q", "quit", "exit"):
            return


def main() -> None:
    ap = argparse.ArgumentParser(description="Device video-codec support -> suggested Convert preset.")
    sub = ap.add_subparsers(dest="cmd")
    lu = sub.add_parser("lookup")
    lu.add_argument("query", nargs="+")
    li = sub.add_parser("list")
    li.add_argument("category", nargs="?")
    sub.add_parser("add")
    sub.add_parser("resources")
    args = ap.parse_args()

    db = load()
    if args.cmd == "lookup":
        cmd_lookup(db, " ".join(args.query))
    elif args.cmd == "list":
        cmd_list(db, args.category)
    elif args.cmd == "add":
        cmd_add(db)
    elif args.cmd == "resources":
        print_resources()
    else:
        menu(db)


if __name__ == "__main__":
    main()
