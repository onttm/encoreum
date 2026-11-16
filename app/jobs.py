"""
jobs.py -- persistent job store (SQLite) + background runner + live logging.

A "job" is one run of the pipeline. Jobs survive container restarts (stored in
/config/jobs.db); their logs stream to /config/logs/<id>.log. Jobs that were
still "running" when the container stopped are marked "interrupted" on startup.
"""
import json
import re
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config, pipeline, presets, preview

# Auto-chunking: a playlist with MORE than this many items gets split into
# sequential jobs of this size each (see start_new()). Chosen so a chunk
# finishes in a few minutes (frequent real feedback) rather than tens of
# minutes (one opaque job).
CHUNK_THRESHOLD = 30
CHUNK_SIZE = 20

# --- module state -----------------------------------------------------------
_db_lock = threading.Lock()
_controls: dict[str, pipeline.JobControl] = {}
_controls_lock = threading.Lock()

TERMINAL = {"done", "partial", "error", "cancelled", "interrupted"}
# Audio presets/audio_only produce presets.AUDIO_EXTS instead of VIDEO_EXTS --
# _has_media used to check ONLY video extensions, so an audio job was ALWAYS
# "error" on any rc!=0 regardless of how much real audio it actually produced
# (found 2026-09-29: a 132-item audiobook job that published only
# .jpg/.info.json sidecars -- zero actual audio, a genuine 100% failure --
# looked IDENTICAL in the UI to a job that fails on its last item after 131
# successes; both just said "error"). presets.MEDIA_EXTS is the shared
# video+audio set (also used by library.py and pipeline.py).


def _has_media(show_dir) -> bool:
    """Real downloaded content exists -- not just metadata sidecars
    (.info.json/.jpg/.png/.webp). Distinguishes "partial" (rc!=0 but SOME
    content landed) from "error" (nothing real did)."""
    try:
        return any(p.suffix.lower() in presets.MEDIA_EXTS
                   for p in Path(show_dir).rglob("*") if p.is_file())
    except OSError:
        return False


# --- error summarization ------------------------------------------------
_ERROR_LINE_RE = re.compile(r"^ERROR:\s*(.+)$")

# Small, deliberately bounded set of recognizable patterns -> plain-language
# explanation. Not trying to cover everything yt-dlp can say -- just the
# handful of cases common enough to be worth explaining instead of leaving
# someone to go read yt-dlp source. Extend as new patterns turn up.
_ERROR_HINTS = [
    (re.compile(r"HTTP Error 403", re.I),
     "YouTube blocked this download (rate-limiting, or a format needing an "
     "auth token this app doesn't have). Often temporary -- try again later "
     "or with a smaller selection."),
    (re.compile(r"Private video", re.I),
     "This video is private and can't be downloaded."),
    (re.compile(r"Video unavailable", re.I),
     "The uploader removed this video, or it's blocked in this region."),
    (re.compile(r"Sign in to confirm", re.I),
     "Age-restricted -- set 'Cookies from browser' to one where you're signed into YouTube."),
    (re.compile(r"copyright", re.I),
     "Blocked or muted for a copyright claim."),
    (re.compile(r"uses DRM|encrypted|content protection", re.I),
     "This player's audio is DRM-protected or encrypted. Page capture doesn't "
     "work around that, and retrying won't change it."),
    (re.compile(r"no audio found", re.I),
     "The player never loaded any audio. If the site needs a login, save a "
     "session first (see README: Page audio capture)."),
    (re.compile(r"answered HTTP (401|403|429)", re.I),
     "The site refused the browser. That's usually a login requirement or a "
     "bot check, and page capture doesn't work around bot checks."),
]


def _hint_for(message: str) -> str | None:
    for pattern, hint in _ERROR_HINTS:
        if pattern.search(message):
            return hint
    return None


def error_summary(job_id: str, limit: int = 5) -> dict:
    """Deduplicated ERROR: lines from this job's log, most common first --
    the difference between "something went wrong, go read a 2000-line log"
    and an actual answer. total_errors counts every occurrence: a 132-item
    playlist failing the same way 132 times is a very different story than
    failing once, and the count alone says so at a glance."""
    path = config.LOGS_DIR / f"{job_id}.log"
    counts: dict[str, int] = {}
    total = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _ERROR_LINE_RE.match(line.strip())
                if not m:
                    continue
                total += 1
                msg = m.group(1).strip()
                counts[msg] = counts.get(msg, 0) + 1
    except OSError:
        return {"total_errors": 0, "distinct": []}
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
    return {
        "total_errors": total,
        "distinct": [{"message": msg, "count": n, "hint": _hint_for(msg)} for msg, n in top],
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    config.ensure_dirs()
    # Clear any staging left behind by a crashed/killed job (safe: no job can be
    # running at startup, and staging lives outside every Plex library folder).
    shutil.rmtree(config.STAGING_DIR, ignore_errors=True)
    with _db_lock, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id         TEXT PRIMARY KEY,
                show       TEXT NOT NULL,
                url        TEXT NOT NULL,
                naming     TEXT NOT NULL,
                status     TEXT NOT NULL,
                created    TEXT NOT NULL,
                finished   TEXT,
                returncode INTEGER,
                show_dir   TEXT,
                params     TEXT NOT NULL
            )
            """
        )
        # Any job left "running"/"queued" from a previous life can't resume its
        # thread, so flag it. Its download-archive means a fresh re-run resumes.
        conn.execute(
            "UPDATE jobs SET status='interrupted', finished=? "
            "WHERE status IN ('running','queued')",
            (_now(),),
        )


class Logger:
    """Line-buffered logger; ``.file`` is handed straight to subprocess stdout."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(path, "a", buffering=1, encoding="utf-8", errors="replace")

    def __call__(self, msg: str = "") -> None:
        self.file.write(msg.rstrip("\n") + "\n")
        self.file.flush()

    def close(self):
        try:
            self.file.close()
        except Exception:
            pass


# --- CRUD -------------------------------------------------------------------
def _insert_job(job_id: str, params: dict, show_dir: Path) -> None:
    with _db_lock, _connect() as conn:
        conn.execute(
            "INSERT INTO jobs (id, show, url, naming, status, created, show_dir, params) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                job_id, params["show"], params["url"],
                params.get("naming", "datecode"), "queued", _now(),
                str(show_dir), json.dumps(params),
            ),
        )


def create_job(params: dict, show_dir: Path) -> str:
    job_id = uuid.uuid4().hex[:12]
    _insert_job(job_id, params, show_dir)
    return job_id


def create_chunked_jobs(params: dict, show_dir: Path, chunks: list[list[int]]) -> list[str]:
    """Create len(chunks) linked jobs -- one per playlist-index batch, all
    sharing the same show/destination/download-archive (so they interleave
    into the same folder with no collisions; the archive is what makes a
    later chunk skip anything an earlier one already got). Only the FIRST is
    started here; each subsequent one auto-starts when the one before it
    reaches a terminal status (see _advance_chunk / _run) -- sequential, not
    parallel, so a big playlist never hits YouTube any harder than one big
    job would (parallel chunks would undo the rate-limit care already built
    into pipeline._common_opts).

    Returns the new job ids, in chunk order.
    """
    total = len(chunks)
    group_id = uuid.uuid4().hex[:12]
    ids = [uuid.uuid4().hex[:12] for _ in chunks]  # pre-generated so each can name the next
    for i, idx_list in enumerate(chunks):
        chunk_params = {
            **params,
            "include": idx_list,
            "_chunk_group": group_id,
            "_chunk_index": i + 1,
            "_chunk_total": total,
            "_chunk_next_id": ids[i + 1] if i + 1 < total else None,
        }
        _insert_job(ids[i], chunk_params, show_dir)
    start_job(ids[0])
    return ids


def chunk_siblings(group_id: str) -> list[dict]:
    """Every job sharing this chunk group, in chunk order -- for cross-linking
    ("part 3 of 7", with links to the others) on the job detail page. The LIKE
    is a pragmatic substring pre-filter (params has no index), followed by an
    exact-match guard in Python since group ids are hex and a false substring
    hit is possible in principle even if very unlikely in practice.
    """
    with _db_lock, _connect() as conn:
        rows = conn.execute(
            "SELECT id, status, params FROM jobs WHERE params LIKE ? ORDER BY created",
            (f'%"_chunk_group": "{group_id}"%',),
        ).fetchall()
    out = []
    for r in rows:
        try:
            p = json.loads(r["params"])
        except (ValueError, TypeError):
            continue
        if p.get("_chunk_group") == group_id:
            out.append({"id": r["id"], "status": r["status"], "index": p.get("_chunk_index")})
    return sorted(out, key=lambda x: x["index"] or 0)


def start_new(params: dict, show_dir: Path) -> list[str]:
    """The one entry point for starting a fresh top-level download: decides
    whether the playlist is large enough to auto-chunk and creates+starts the
    right thing either way. Shared by main.py's /jobs handler AND retry_job()
    -- these used to diverge (retry called create_job() directly), which
    meant retrying a 132-item job re-ran it as one big opaque job again
    instead of getting the same chunking a fresh submission would (found
    2026-09-29 retrying the real job that prompted chunking in the first
    place). Never chunks a live capture (a single stream, not a playlist).

    Returns the new job id(s) in order -- always at least one; index 0 is
    what a caller should redirect/link to.
    """
    # A page capture is one page, not a yt-dlp playlist: flat_count() can't read
    # it, and there is no playlist index for a chunk's --playlist-items to use.
    if not params.get("live") and not params.get("page_capture"):
        item_list = params.get("include")
        if not item_list:
            try:
                total = preview.flat_count(params["url"], params.get("cookies_browser", ""))
            except Exception:
                total = 0
            item_list = list(range(1, total + 1)) if total > CHUNK_THRESHOLD else None
        if item_list and len(item_list) > CHUNK_THRESHOLD:
            chunks = [item_list[i:i + CHUNK_SIZE] for i in range(0, len(item_list), CHUNK_SIZE)]
            return create_chunked_jobs(params, show_dir, chunks)
    job_id = create_job(params, show_dir)
    start_job(job_id)
    return [job_id]


def _update(job_id: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with _db_lock, _connect() as conn:
        conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))


def get_job(job_id: str) -> dict | None:
    with _db_lock, _connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs(limit: int = 100) -> list[dict]:
    with _db_lock, _connect() as conn:
        rows = conn.execute(
            "SELECT id, show, url, naming, status, created, finished, returncode, params "
            "FROM jobs ORDER BY created DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        raw = d.pop("params", None)
        try:
            p = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            p = {}
        if p.get("_chunk_total"):
            d["chunk_label"] = f"part {p.get('_chunk_index')} of {p.get('_chunk_total')}"
        out.append(d)
    return out


def get_status(job_id: str) -> str | None:
    job = get_job(job_id)
    return job["status"] if job else None


def active_summary() -> dict:
    """{running, live, job_id} for the 'On Air' indicator (job_id = newest running)."""
    with _db_lock, _connect() as conn:
        rows = conn.execute(
            "SELECT id, params FROM jobs WHERE status IN ('running','queued') "
            "ORDER BY created DESC"
        ).fetchall()
    live = False
    for r in rows:
        try:
            if json.loads(r["params"]).get("live"):
                live = True
                break
        except (ValueError, TypeError):
            continue
    return {"running": len(rows), "live": live, "job_id": rows[0]["id"] if rows else None}


# --- running ----------------------------------------------------------------
def start_job(job_id: str) -> None:
    """Kick off the background thread that runs the pipeline for ``job_id``."""
    threading.Thread(target=_run, args=(job_id,), daemon=True).start()


def _run(job_id: str) -> None:
    job = get_job(job_id)
    if not job:
        return
    params = json.loads(job["params"])
    show_dir = Path(job["show_dir"])
    control = pipeline.JobControl()
    with _controls_lock:
        _controls[job_id] = control

    log = Logger(config.LOGS_DIR / f"{job_id}.log")
    _update(job_id, status="running")
    status = "error"   # safe default; every branch below overwrites it
    try:
        rc = pipeline.run_job(params, show_dir, control, log, job_id)
        # rc != 0 usually means individual videos were skipped (e.g. one removed by
        # the uploader), not a real failure — call it "partial" if we produced media.
        if rc == 0:
            status = "done"
        elif _has_media(show_dir):
            status = "partial"
        else:
            status = "error"
        _update(job_id, status=status, returncode=rc, finished=_now())
    except pipeline.Cancelled:
        status = "cancelled"
        log("")
        log("*** Job cancelled by user. ***")
        _update(job_id, status=status, finished=_now())
    except Exception as exc:  # noqa: BLE001 - surface any failure to the log/UI
        status = "error"
        log("")
        log(f"*** ERROR: {exc} ***")
        _update(job_id, status=status, finished=_now())
    finally:
        log.close()
        with _controls_lock:
            _controls.pop(job_id, None)
    _advance_chunk(params, status)


def _advance_chunk(params: dict, status: str) -> None:
    """If this job was one chunk of a larger split playlist, start the next
    one -- sequential by design (see create_chunked_jobs). A user-requested
    stop (cancelled) or an ambiguous one (interrupted -- e.g. the container
    restarted) does NOT auto-advance: silently continuing a chain after an
    explicit or unclear stop would be a surprise, not a convenience. done/
    partial/error all continue -- one chunk's trouble shouldn't block the rest,
    matching this app's existing --ignore-errors philosophy."""
    if status in ("cancelled", "interrupted"):
        return
    next_id = params.get("_chunk_next_id")
    if next_id:
        start_job(next_id)


def cancel_job(job_id: str) -> bool:
    """Cancel a running job. Returns True if it was running."""
    with _controls_lock:
        control = _controls.get(job_id)
    if control is None:
        return False
    control.cancel()
    return True


def retry_job(job_id: str) -> str | None:
    """Start a NEW job with the ORIGINAL job's exact params (same show, so the
    same --download-archive file). Deliberately does NOT try to figure out
    which playlist items failed -- the archive already knows: yt-dlp only
    writes an entry there once a video FULLY completes, so anything that
    404'd/403'd/etc. was never recorded and gets attempted again, while
    anything that already succeeded is skipped. That's "download only the
    failed items" for free, correctly, without this app maintaining its own
    (necessarily heuristic, log-parsing-based) idea of what failed.

    Retrying the ORIGINAL job of a large playlist (never chunked, or a whole
    playlist resubmitted) goes back through start_new(), so it gets chunked
    just like a fresh submission would -- these used to diverge (this
    function called create_job() directly), so retrying a 132-item job
    re-ran it as one big job again instead of getting the same resilience.
    Retrying a SINGLE chunk stays a single job automatically, because that
    chunk's own `include` slice is already at or under CHUNK_THRESHOLD --
    start_new() doesn't need to treat the two cases differently.

    Returns the new job's id (the first one, if this triggers chunking), or
    None if the original job doesn't exist. Never mutates or deletes the
    original job/log -- it stays as the historical record of that attempt.
    """
    job = get_job(job_id)
    if not job:
        return None
    params = json.loads(job["params"])
    # If this was one chunk of a larger split, keep the group/index labeling
    # (so the retry still shows "part 3 of 7") but drop _chunk_next_id -- the
    # chain already advanced past this chunk when it first finished, so
    # re-running it must NOT re-trigger starting the next one all over again.
    params.pop("_chunk_next_id", None)
    params["_retry_of"] = job_id
    ids = start_new(params, Path(job["show_dir"]))
    return ids[0]


def list_output_files(job_id: str) -> list[str]:
    """Files currently present under the job's show folder (for the detail page)."""
    job = get_job(job_id)
    if not job or not job.get("show_dir"):
        return []
    root = Path(job["show_dir"])
    if not root.is_dir():
        return []
    files = [p for p in root.rglob("*") if p.is_file()]
    return sorted(str(p.relative_to(root)) for p in files)
