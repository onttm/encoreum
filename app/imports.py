"""
imports.py -- the review queue that stands between an ingest folder and Plex.

THE RULE THIS MODULE EXISTS TO ENFORCE
    A MANUAL upload is never written to the library without a person saying yes.

    Not "not written when confidence is low" — never. Someone who hand-carried
    an archive from a taper community expects to see what it was matched to
    before it lands, and confidence is exactly the wrong gate for that: a
    confident wrong match is the case where review matters MOST, because it is
    the one nothing else will catch. The tagger can be certain that twelve files
    map to twelve titles and still be certain about the wrong show.

    So origin decides eligibility, not confidence:

        origin='manual'   dropped in the ingest folder, or uploaded
                          -> ALWAYS needs review. No setting turns this off.
        origin='auto'     produced by a capture this app scheduled and ran
                          -> may import unattended when the mapping is EXACT

    That asymmetry is the whole point. An automated capture already knows what
    it recorded — it chose the artist, the date and the stream itself, so there
    is no external claim to verify. A manual upload carries a claim from
    somewhere else, and verifying it is the human's job.

EVERYTHING ELSE IS AUTOMATED
    Review is one decision, not a chore. Finding new drops, unpacking, reading
    info files and embedded tags, parsing setlists, aligning them against real
    audio durations, proposing artist/date/venue/source — all of it happens
    before anyone looks, so review is "yes, that is the show" and not data
    entry. The queue holds a finished proposal, never a form to fill in.

FIXING A BAD MATCH
    Overrides are stored SEPARATELY from the proposal and re-applied on every
    re-inspect, so a correction survives a rescan and it stays obvious which
    fields a human touched. Clearing an override returns the field to whatever
    the parser proposes now.

    This mirrors artist-overrides.json in scripts/artist-lookup.py, for the same
    reason stated there: fixing the same thing twice is a bug.

WHY IT SHELLS OUT
    ingest-taper.py stays runnable on its own — the standalone rule in
    CLAUDE.md — so this module calls it as a subprocess with --json rather than
    importing it. The script is the single implementation of the tagging logic;
    this module only decides when it may write.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import config

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ingest-taper.py"

# Where uploads land. Watched, not owned: a person drops a folder or a zip here
# by whatever means they like — scp, a share, a browser upload — and the scanner
# picks it up. Deliberately OUTSIDE the library so nothing here is visible to
# Plex until it has been approved.
INGEST_DIR = Path(os.environ.get("INGEST_DIR", "/ingest"))

# The Live Music audio library. Separate from the video Concerts library because
# live tracks mixed into a studio library ruin shuffle and PlexAmp DJing.
LIVE_AUDIO_DIR = Path(os.environ.get("LIVE_AUDIO_DIR",
                                     str(config.LIBRARY_ROOT / "live-music")))

# Human workflow, kept on its own axis from where the files actually are —
# the same split catalog.py draws between requests.status and library_status.
PENDING, APPROVED, IMPORTED, REJECTED, FAILED = (
    "pending", "approved", "imported", "rejected", "failed")

MANUAL, AUTO = "manual", "auto"

# Fields a human may correct. Anything not listed here is derived and cannot be
# hand-set — there is no override for the track mapping itself, because the way
# to fix a mapping is to supply the right setlist and let it re-align against
# the real durations.
OVERRIDABLE = ("artist", "date", "venue", "city", "source_type",
               "setlist", "cross_link", "note")


def _override_args(ov: dict) -> list[str]:
    """Turn stored corrections into tagger flags.

    Built once and used by BOTH the preview and the write. They drifted apart
    exactly once — the preview honoured a corrected city, the write did not, so
    the queue showed a field as fixed while the file it produced still carried
    the parser's guess. A correction that silently fails to apply is worse than
    one that is refused, because nothing announces it.
    """
    args = []
    for k in ("artist", "date", "venue", "city"):
        if ov.get(k):
            args += [f"--{k}", str(ov[k])]
    if ov.get("source_type"):
        args += ["--source-type", str(ov["source_type"])]
    if ov.get("cross_link"):
        args += ["--cross-link", str(ov["cross_link"])]
    return args


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS imports (
            id           TEXT PRIMARY KEY,
            path         TEXT NOT NULL,
            container    TEXT,
            origin       TEXT NOT NULL DEFAULT 'manual',
            status       TEXT NOT NULL DEFAULT 'pending',

            -- fingerprint: how we know a folder changed and is worth re-reading
            fingerprint  TEXT,

            -- what the parser proposed, verbatim, and what a human corrected.
            -- Kept apart on purpose: merging them would lose track of which
            -- fields were touched, and a rescan would silently revert them.
            proposed     TEXT NOT NULL DEFAULT '{}',
            overrides    TEXT NOT NULL DEFAULT '{}',

            confidence   TEXT,
            uploaded_by  TEXT,
            discovered_at TEXT NOT NULL,
            reviewed_by  TEXT,
            reviewed_at  TEXT,
            album_path   TEXT,
            error        TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS imports_path ON imports(path);
        CREATE INDEX IF NOT EXISTS imports_status ON imports(status);
        """)


# --------------------------------------------------------------------------
# inspection
# --------------------------------------------------------------------------

def _fingerprint(p: Path) -> str:
    """Cheap 'has this changed' stamp: newest mtime + file count + total size.

    Enough to notice a still-copying upload growing between scans, which is the
    failure this prevents — inspecting a half-transferred folder proposes a
    truncated tracklist that looks perfectly confident.
    """
    if p.is_file():
        st = p.stat()
        return f"f:{int(st.st_mtime)}:{st.st_size}"
    newest, count, total = 0.0, 0, 0
    for f in p.rglob("*"):
        if f.is_file():
            st = f.stat()
            newest = max(newest, st.st_mtime)
            total += st.st_size
            count += 1
    return f"d:{int(newest)}:{count}:{total}"


def inspect(path: Path, overrides: dict | None = None) -> dict:
    """Run the standalone tagger in read-only mode and return its proposal."""
    ov = overrides or {}
    cmd = [sys.executable, str(SCRIPT), str(path), "--json"] + _override_args(ov)
    setlist = (ov.get("setlist") or "").strip()
    if setlist:
        cmd += ["--setlist", "-"]
    try:
        r = subprocess.run(cmd, input=setlist if setlist else None,
                           capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return {"error": "inspection timed out"}
    if r.returncode != 0:
        return {"error": (r.stderr or "inspection failed").strip()[:500]}
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"error": "tagger returned unreadable output"}
    return out[0] if out else {"error": "no result"}


# --------------------------------------------------------------------------
# the queue
# --------------------------------------------------------------------------

def scan(ingest_dir: Path | None = None, origin: str = MANUAL,
         uploaded_by: str = "") -> dict:
    """Find new or changed drops and queue them for review. Safe to re-run.

    This is the automated half. It never writes to the library and never
    approves anything — it only turns a folder someone dropped into a finished
    proposal waiting for one yes/no.
    """
    root = ingest_dir or INGEST_DIR
    if not root.is_dir():
        return {"ok": False, "error": f"ingest folder not found: {root}"}

    seen, added, updated, unchanged, skipped = [], 0, 0, 0, []
    for entry in sorted(root.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.is_file() and entry.suffix.lower() not in (".zip",):
            continue
        fp = _fingerprint(entry)
        seen.append(str(entry))
        row = get_by_path(str(entry))

        if row and row["fingerprint"] == fp:
            # Already queued and unchanged. Never re-inspect an import a human
            # has already decided on — that would resurrect rejected drops and
            # quietly re-propose over corrections.
            unchanged += 1
            continue
        if row and row["status"] in (IMPORTED, REJECTED):
            skipped.append({"path": str(entry), "status": row["status"],
                            "why": "already decided; changed on disk since"})
            continue

        # Still copying? The fingerprint moved since a moment ago. Leave it for
        # the next pass rather than proposing a truncated tracklist.
        time.sleep(0.4)
        if _fingerprint(entry) != fp:
            skipped.append({"path": str(entry), "why": "still being written"})
            continue

        ov = json.loads(row["overrides"]) if row else {}
        r = inspect(entry, ov)
        conf = (r.get("alignment") or {}).get("confidence")
        rec = {
            "path": str(entry),
            "container": r.get("container") or ("zip" if entry.is_file() else "folder"),
            "origin": origin,
            "fingerprint": fp,
            "proposed": json.dumps(r, default=str),
            "confidence": conf,
            "error": r.get("error"),
        }
        if row:
            _update(row["id"], **rec)
            updated += 1
        else:
            rec.update({"uploaded_by": uploaded_by, "overrides": json.dumps(ov)})
            _insert(rec)
            added += 1

    return {"ok": True, "scanned": len(seen), "added": added,
            "updated": updated, "unchanged": unchanged, "skipped": skipped,
            "pending": count_pending()}


def _insert(rec: dict) -> str:
    iid = uuid.uuid4().hex[:12]
    with _connect() as conn:
        conn.execute(
            "INSERT INTO imports (id, path, container, origin, status, "
            "fingerprint, proposed, overrides, confidence, uploaded_by, "
            "discovered_at, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (iid, rec["path"], rec.get("container"), rec.get("origin", MANUAL),
             PENDING, rec.get("fingerprint"), rec.get("proposed", "{}"),
             rec.get("overrides", "{}"), rec.get("confidence"),
             rec.get("uploaded_by", ""), _now(), rec.get("error")))
    return iid


def _update(iid: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with _connect() as conn:
        conn.execute(f"UPDATE imports SET {cols} WHERE id=?",
                     (*fields.values(), iid))


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["proposed"] = json.loads(d.get("proposed") or "{}")
    d["overrides"] = json.loads(d.get("overrides") or "{}")
    # The effective view: what would actually be written right now. The UI shows
    # this, and flags which fields a human changed so a correction is visible
    # rather than blending into the parser's output.
    eff = {}
    for k in ("artist", "date", "venue", "city", "source_type", "cross_link"):
        eff[k] = d["overrides"].get(k) or d["proposed"].get(k)
    d["effective"] = eff
    d["corrected_fields"] = sorted(k for k, v in d["overrides"].items()
                                   if v not in (None, "") and k != "note")
    d["needs_review"] = d["status"] == PENDING
    d["auto_importable"] = auto_importable(d)
    return d


def get(iid: str) -> dict | None:
    with _connect() as conn:
        r = conn.execute("SELECT * FROM imports WHERE id=?", (iid,)).fetchone()
    return _row(r) if r else None


def get_by_path(path: str) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute("SELECT * FROM imports WHERE path=?",
                            (path,)).fetchone()


def listing(status: str = "", limit: int = 100) -> list[dict]:
    q = "SELECT * FROM imports"
    args: tuple = ()
    if status:
        q += " WHERE status=?"
        args = (status,)
    q += " ORDER BY discovered_at DESC LIMIT ?"
    with _connect() as conn:
        return [_row(r) for r in conn.execute(q, (*args, limit))]


def count_pending() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM imports WHERE status=?",
                            (PENDING,)).fetchone()[0]


def auto_importable(d: dict) -> bool:
    """Whether this import may be written WITHOUT a human.

    Manual uploads: never. See the module docstring — this is the invariant the
    module exists for, so it is a return False and not a config lookup.
    """
    if d.get("origin") != AUTO:
        return False
    if d.get("error"):
        return False
    return d.get("confidence") == "exact"


# --------------------------------------------------------------------------
# fixing a bad match
# --------------------------------------------------------------------------

def set_overrides(iid: str, by: str = "", **fields) -> dict:
    """Correct a proposal and immediately re-inspect under the correction.

    Re-inspecting here rather than at approval time is deliberate: the person
    fixing the match gets to SEE the corrected alignment before committing. A
    fix that silently made the mapping worse would otherwise only surface after
    the files were already in the library.

    Pass an empty string to clear a field and fall back to the parser.
    """
    row = get(iid)
    if not row:
        return {"ok": False, "error": "no such import"}
    if row["status"] == IMPORTED:
        return {"ok": False, "error": "already imported — cannot re-match in "
                                      "place; remove it from the library first"}
    ov = dict(row["overrides"])
    for k, v in fields.items():
        if k not in OVERRIDABLE:
            return {"ok": False, "error": f"{k} is not correctable"}
        if v is None:
            continue
        if str(v).strip() == "":
            ov.pop(k, None)          # explicit clear -> back to the parser
        else:
            ov[k] = str(v).strip() if k != "setlist" else str(v)

    r = inspect(Path(row["path"]), ov)
    _update(iid,
            overrides=json.dumps(ov),
            proposed=json.dumps(r, default=str),
            confidence=(r.get("alignment") or {}).get("confidence"),
            error=r.get("error"),
            status=PENDING,          # a correction always returns it to review
            reviewed_by=by or row.get("reviewed_by"),
            reviewed_at=_now())
    return {"ok": True, "import": get(iid)}


# --------------------------------------------------------------------------
# the decision
# --------------------------------------------------------------------------

def approve(iid: str, by: str, force: bool = False,
            out_dir: Path | None = None) -> dict:
    """Write an approved import into the Live Music library.

    `by` is required. An import that lands in the library with no name attached
    cannot be explained later, and "who approved this" is the first question
    asked when a wrong album shows up.
    """
    if not by:
        return {"ok": False, "error": "approval requires a reviewer"}
    row = get(iid)
    if not row:
        return {"ok": False, "error": "no such import"}
    if row["status"] == IMPORTED:
        return {"ok": False, "error": "already imported", "album": row["album_path"]}
    if row.get("error"):
        return {"ok": False, "error": f"cannot import: {row['error']}"}

    src = Path(row["path"])
    if not src.exists():
        return {"ok": False, "error": "the upload is gone from the ingest folder"}

    work_dir = src
    tmp: Path | None = None
    if src.is_file():
        # The tagger writes from a folder, so a zip is unpacked next to it.
        # Unpacking at APPROVAL rather than at scan keeps the ingest folder
        # clean while things sit in review.
        tmp = src.with_suffix("")
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        shutil.unpack_archive(str(src), str(tmp))
        inner = [p for p in tmp.iterdir() if p.is_dir()]
        work_dir = inner[0] if len(inner) == 1 and not any(
            p.is_file() for p in tmp.iterdir()) else tmp

    ov = dict(row["overrides"])
    dest = out_dir or LIVE_AUDIO_DIR
    dest.mkdir(parents=True, exist_ok=True)

    cmd = ([sys.executable, str(SCRIPT), str(work_dir), "--json",
            "--out", str(dest), "--apply"] + _override_args(ov))
    setlist = (ov.get("setlist") or "").strip()
    if setlist:
        cmd += ["--setlist", "-"]
    if force or row["confidence"] == "ambiguous":
        # Approving an ambiguous mapping is allowed — the human looked at it and
        # said yes, which is exactly the authority --force was built to carry.
        cmd += ["--force"]

    r = subprocess.run(cmd, input=setlist if setlist else None,
                       capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        _update(iid, status=FAILED, error=(r.stderr or "").strip()[:500],
                reviewed_by=by, reviewed_at=_now())
        return {"ok": False, "error": (r.stderr or "write failed").strip()[:500]}

    try:
        res = json.loads(r.stdout)[0]
    except (json.JSONDecodeError, IndexError):
        _update(iid, status=FAILED, error="tagger returned unreadable output")
        return {"ok": False, "error": "tagger returned unreadable output"}

    w = res.get("write") or {}
    if not w.get("ok"):
        _update(iid, status=FAILED, error=w.get("error", "write failed"),
                reviewed_by=by, reviewed_at=_now())
        return {"ok": False, "error": w.get("error", "write failed")}

    _update(iid, status=IMPORTED, album_path=w.get("album"),
            proposed=json.dumps(res, default=str), error=None,
            reviewed_by=by, reviewed_at=_now())
    if tmp and tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    return {"ok": True, "album": w.get("album"), "written": w.get("written"),
            "import": get(iid)}


def reject(iid: str, by: str, reason: str = "") -> dict:
    if not by:
        return {"ok": False, "error": "rejection requires a reviewer"}
    row = get(iid)
    if not row:
        return {"ok": False, "error": "no such import"}
    ov = dict(row["overrides"])
    if reason:
        ov["note"] = reason
    _update(iid, status=REJECTED, overrides=json.dumps(ov),
            reviewed_by=by, reviewed_at=_now())
    return {"ok": True, "import": get(iid)}


def auto_import_pass(by: str = "auto") -> dict:
    """Import the AUTO-origin, exact-confidence backlog unattended.

    Manual uploads are structurally excluded by auto_importable(), so this can
    never drain the human review queue no matter what is sitting in it.
    """
    done, skipped = [], 0
    for d in listing(PENDING, limit=200):
        if not d["auto_importable"]:
            skipped += 1
            continue
        r = approve(d["id"], by=by)
        done.append({"id": d["id"], "ok": r["ok"],
                     "album": r.get("album"), "error": r.get("error")})
    return {"ok": True, "imported": done, "left_for_review": skipped}
