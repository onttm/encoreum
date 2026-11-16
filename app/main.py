"""
main.py -- FastAPI app: the web UI, the JSON/API surface, and the live-log SSE.
"""
import asyncio
import subprocess
import json
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import (auth, captures, catalog, config, discovery, history, imports, jobs,
               library, pipeline, plexlib, presets, preview, ratelimit, schedule,
               secrets, setlistfm, settings, sponsorblock, updater)

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))

# Placeholder show name for a /preview run with no name chosen yet (the
# suggestion chips are what a blank Show field is FOR). Never written to
# disk -- /preview never writes anything regardless.
_UNTITLED = "(untitled)"
# Audiobook preset only: placeholder for the Author folder segment when no
# Author has been typed/picked yet. Always used for audiobook (the Author/
# Title layout is unconditional, not only when an author happens to be
# filled in) -- see _build_job_params.
_UNKNOWN_AUTHOR = "(unknown author)"


def _folder_id(naming: str, show: str, author: str = "") -> str:
    """The identifier passed to config.resolve_show_dir()/library.show_info():
    plain Title for every preset except audiobook, which nests Author/Title
    unconditionally. Shared by _build_job_params (the real job) and
    /api/show-info (the live "folder exists?" check the UI shows while
    typing) so the two can never drift apart -- they did, briefly: the
    existence-check and "Files go to" hint kept showing the OLD flat path
    after Author shipped, because nothing told them about it."""
    if naming == "audiobook":
        return f"{author.strip() or _UNKNOWN_AUTHOR}/{show}"
    return show

app = FastAPI(title="Encoreum")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

_scheduler = BackgroundScheduler()


@app.on_event("startup")
def _startup() -> None:
    config.ensure_dirs()
    jobs.init_db()
    catalog.init_db()
    imports.init_db()
    captures.init_db()
    ratelimit.init_db()
    # Daily yt-dlp self-update (in addition to the one on every container start).
    _scheduler.add_job(updater.update_ytdlp, "interval", hours=24, id="daily-ytdlp-update")

    # Concert-queue discovery. 30 min matches feed-reader norms; jitter is not
    # cosmetic — perfectly regular timing is itself a bot signal, and neither
    # the feed nor the yt-dlp endpoint publishes a rate limit, so behaving well
    # IS the protection. coalesce+max_instances keep a slow pass from stacking.
    _scheduler.add_job(discovery.poll_once, "interval", minutes=30,
                       id="concert-queue-poll", jitter=300,
                       coalesce=True, max_instances=1,
                       next_run_time=datetime.now() + timedelta(seconds=45))
    # Ingest scanning. Finding and parsing a drop is automatable, so it is
    # automated; deciding it is correct is not, so it is not. A pass only ever
    # moves an upload into the review queue.
    _scheduler.add_job(imports.scan, "interval", minutes=10, id="ingest-scan",
                       jitter=60, coalesce=True, max_instances=1,
                       next_run_time=datetime.now() + timedelta(seconds=60))
    # Setlist enrichment. Hourly is generous for something whose retry schedule
    # is measured in hours and days; the pass is bounded per run so a backlog
    # cannot monopolise a rate limit shared with every other capture. Skips
    # itself entirely when no API key is set.
    _scheduler.add_job(captures.enrich_pass, "interval", minutes=60,
                       id="setlist-enrich", jitter=600, coalesce=True,
                       max_instances=1,
                       next_run_time=datetime.now() + timedelta(minutes=3))
    _scheduler.start()


@app.on_event("shutdown")
def _shutdown() -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)


# --- helpers ----------------------------------------------------------------
def _truthy(v) -> bool:
    return str(v).lower() in ("1", "true", "on", "yes")


# --- pages ------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "jobs": jobs.list_jobs(),
            "defaults": config.DEFAULTS,
            "library_root": str(config.LIBRARY_ROOT),
            "destination": settings.destination(),
            "media_folders": [e["name"] for e in library.browse("").get("entries", [])],
            "browsers": sorted(preview.SUPPORTED_BROWSERS),
            "video_presets": [(k, presets.label(k)) for k in presets.VIDEO_NAMES],
            "audio_presets": [(k, presets.label(k)) for k in presets.AUDIO_NAMES
                              if not presets.is_addon(k)],
            "addon_presets": [(k, presets.label(k)) for k in presets.ADDON_NAMES
                              if k in settings.addons_enabled()],
            "addons": [{"name": k, "label": presets.label(k), "blurb": presets.addon_blurb(k),
                        "on": k in settings.addons_enabled()} for k in presets.ADDON_NAMES],
            "audio_names": list(presets.AUDIO_NAMES),
            "preset_dest": {k: presets.default_dest(k) for k in presets.AUDIO_NAMES},
            "yt_dlp": updater.ytdlp_version(),
        },
    )


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: str):
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    params = json.loads(job["params"])
    if params.get("_chunk_total"):
        job["chunk_label"] = f"Part {params['_chunk_index']} of {params['_chunk_total']}"
        job["chunk_siblings"] = jobs.chunk_siblings(params["_chunk_group"])
    return templates.TemplateResponse(
        request, "job.html", {"job": job}
    )


# --- API --------------------------------------------------------------------
def _parse_include(include: str) -> list[int] | None:
    """Parse a comma-separated list of playlist indices; None = all items."""
    include = (include or "").strip()
    if not include:
        return None
    out = []
    for tok in include.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if not tok.isdigit():
            raise HTTPException(400, "Selection must be whole playlist numbers.")
        out.append(int(tok))
    return out or None


import re as _re
import shlex as _shlex
_RATE_RE = _re.compile(r"^\d+(\.\d+)?[KMG]?$", _re.I)
_TIME_RE = _re.compile(r"^-?[\d:.]+$")  # leading '-' allowed: yt-dlp counts negative times from the END

# yt-dlp flags that run programs, write outside the job folder, or read server files.
# These are NOT download features — blocking them removes no downloading capability but
# keeps RCE / path-escape / file-read off the table (even for sandboxed standard users).
_DENIED_OPTS = {
    # run arbitrary commands
    "--exec", "--exec-before-download", "--exec-after-move",
    "--use-postprocessor", "--postprocessor-args", "--ppa",
    "--external-downloader", "--downloader", "--downloader-args",
    "--plugin-dirs",
    # write / read outside the sandbox
    "-o", "--output", "-P", "--paths", "--cache-dir", "--ffmpeg-location",
    "--cookies", "--batch-file", "-a", "--load-info-json",
    "--config-location", "--config", "--download-archive", "--home", "--print-to-file",
}

# Options that are safe but wrong for this library, refused with a reason that
# says what to do instead (rather than the generic sandbox message above).
_REFUSED_OPTS = {
    # Chapter MARKS are already embedded (--embed-chapters is always on), which
    # is what Plex reads. Splitting writes one file per song: the chapter files
    # get no -o template, so yt-dlp wrote them into the cwd (/app, root-owned)
    # and failed "Permission denied" on every item of a 27-show Band A job
    # (2026-11-16). Even with a path, per-song files would flood a Concerts-as-TV
    # season folder as bogus episodes. Per-song AUDIO albums are a separate tool:
    # scripts/rip-concert-audio.py.
    "--split-chapters": "Chapter marks are already embedded in every download, which is "
                        "what Plex shows as chapters. --split-chapters would cut each video "
                        "into one file per song, which this library doesn't support. "
                        "Remove it and submit again.",
}


def _parse_clip(clip: str) -> str:
    """'1:30-5:00' (a range) / '24' (skip the first 24s) / '-30' (TRIM THE LAST
    30s -- an outro/promo) -> yt-dlp --download-sections value '*start-end'.

    yt-dlp itself treats a negative timestamp as "N before the end" (verified
    against a real video: '*0--5' on a 19s video produced a 14s file). The
    only real work here is not mistaking a negative END's own '-' sign for the
    start/end separator -- a naive "split on the first dash" gets '-30' wrong
    (parses as start='', end='30' -> keeps only the FIRST 30s, the opposite of
    what the field's own placeholder has always advertised for that input).
    """
    clip = (clip or "").strip()
    if not clip:
        return ""
    if clip.startswith("-"):
        # The whole value IS a negative end offset; start defaults to 0.
        start, end = "0", clip
    elif "-" in clip[1:]:
        # Split on the first '-' AFTER position 0, so a negative END's own
        # sign (e.g. "10--30" = from 10s to 30s-before-the-end) is never
        # mistaken for the separator.
        idx = clip.index("-", 1)
        start, end = clip[:idx], clip[idx + 1:]
    else:
        start, end = clip, "inf"
    start, end = (start.strip() or "0"), (end.strip() or "inf")
    for v in (start, end):
        if v != "inf" and not _TIME_RE.match(v):
            raise HTTPException(400, "Clip times must be like 90, 1:30, 1:30:00, or -30 "
                                     "(e.g. '1:30-5:00', '24', or '-30' to trim the last 30s).")
    return f"*{start}-{end}"


def _parse_extra_opts(s: str) -> list:
    s = (s or "").strip()
    if not s:
        return []
    try:
        toks = _shlex.split(s)
    except ValueError:
        raise HTTPException(400, "Couldn't parse advanced options (check your quotes).")
    for t in toks:
        base = t.split("=", 1)[0]
        if base in _REFUSED_OPTS:
            raise HTTPException(400, f"Option '{base}' isn't allowed here: {_REFUSED_OPTS[base]}")
        if base in _DENIED_OPTS:
            raise HTTPException(400, f"Option '{base}' isn't allowed here "
                                     "(it would override output paths / run commands).")
    return toks


def _build_job_params(*, url, show, season, naming, reverse, quality, write_subs,
                      sponsor_marks="",
                      cookies_browser, destination, run_rename, run_nfo, run_artwork,
                      genres, studio, include="", limit_rate="", live="", direct="",
                      clip="", ytdlp_opts="", tour="", require_show=True, audio_only="",
                      author="", page_capture=""):
    """Validate raw form values and return (params, show_dir). Shared by /jobs and /preview.

    require_show=False is for /preview only: a dry run should be usable
    BEFORE you've decided on a show name (that's what the name-suggestion
    chips are for) -- a real /jobs download always needs a real folder name.

    audio_only is DIFFERENT from an audio preset (presets.is_audio): a preset
    is its own content type with its own destination/organization scheme and
    no post-processing at all. audio_only is a plain checkbox available on
    ANY naming mode -- "keep this naming mode's dating/venue/destination
    logic, just extract audio instead of video". Only NFO/artwork (genuinely
    video-only, Plex/Kodi-specific) get skipped for it; rename/renumber keep
    running, because MEDIA_EXTS in the scripts they call now recognises audio
    containers too (see rename-by-footage-date.py).
    """
    if naming not in presets.NAMES:
        raise HTTPException(400, f"Naming must be one of: {', '.join(presets.NAMES)}.")
    if quality not in ("max", "compatible"):
        raise HTTPException(400, "Quality must be max or compatible.")
    is_audio = presets.is_audio(naming)
    wants_audio_only = is_audio or _truthy(audio_only)
    if wants_audio_only and _truthy(live):
        # _capture_live() is a video-only path (streamlink -> .mkv); neither
        # an audio preset nor a plain "audio only" download is a live capture.
        what = presets.label(naming) if is_audio else "Audio only"
        raise HTTPException(400, f"{what} doesn't support live capture.")
    url, show, season = url.strip(), show.strip(), season.strip()
    if not url:
        raise HTTPException(400, "A playlist or video URL is required.")
    wants_page_capture = _truthy(page_capture)
    if wants_page_capture:
        # These fields only mean something to yt-dlp. Rejecting them is better
        # than quietly ignoring a speed limit or clip the person asked for.
        if not is_audio:
            raise HTTPException(400, "Page audio capture needs an audio preset "
                                     "(Audiobook, Podcast or Music library).")
        for value, field in ((limit_rate, "Speed limit"), (clip, "Clip range"),
                             (ytdlp_opts, "Advanced yt-dlp options"),
                             (cookies_browser, "Cookies from browser")):
            if (value or "").strip():
                raise HTTPException(400, f"{field} doesn't apply to page audio capture. "
                                         "Clear it, or turn page capture off.")
        if _truthy(direct):
            raise HTTPException(400, "Page audio capture always uses staging; turn off "
                                     "'Work directly in library'.")
        if not url.lower().startswith(("http://", "https://")):
            raise HTTPException(400, "Page audio capture needs an http(s) page URL.")
        problem = pipeline.page_capture_unavailable()
        if problem:
            raise HTTPException(400, f"{problem} Downloads through yt-dlp still work.")
    if not show:
        if require_show:
            raise HTTPException(400, "A show (artist) name is required.")
        show = _UNTITLED
    if season and not season.isdigit():
        raise HTTPException(400, "Season must be a whole number (e.g. 72).")
    cb = cookies_browser.strip()
    if cb and cb.split(":", 1)[0].lower() not in preview.SUPPORTED_BROWSERS:
        raise HTTPException(
            400, f"Unrecognised browser '{cb}'. Use one of: "
                 f"{', '.join(sorted(preview.SUPPORTED_BROWSERS))}.")
    rate = (limit_rate or "").strip()
    if rate and not _RATE_RE.match(rate):
        raise HTTPException(400, "Speed limit must look like 4M, 800K, or 1.5M.")

    # Audio presets have no season/rename/NFO/artwork concepts (those are all
    # TV-library ideas) and default to their OWN destination rather than
    # whatever folder the app is currently saved to for concerts.
    dest = config.clean_dest(destination) or (presets.default_dest(naming) if is_audio else settings.destination())
    is_audiobook = naming == "audiobook"
    author = author.strip()
    # "show" stays the Title alone (used as the per-track filename prefix by
    # pipeline.flat_audio_template, which takes show and show_dir as
    # independent arguments already) -- only the FOLDER path gets the author.
    folder_id = _folder_id(naming, show, author)
    try:
        show_dir = config.resolve_show_dir(folder_id, dest)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    params = {
        "destination": dest,
        "url": url,
        "show": show,
        "author": author if is_audiobook else "",
        "season": season,
        "naming": naming,
        "reverse": _truthy(reverse),
        "quality": quality,
        "write_subs": _truthy(write_subs),
        "sponsor_marks": _truthy(sponsor_marks),
        "cookies_browser": cb,
        "run_rename": False if is_audio else _truthy(run_rename),
        # NFO/artwork are video-only regardless of WHY this job is audio --
        # rename/renumber stay governed by is_audio alone (see docstring).
        "run_nfo": False if wants_audio_only else _truthy(run_nfo),
        "run_artwork": False if wants_audio_only else _truthy(run_artwork),
        "audio_only": wants_audio_only,
        "genres": [g.strip() for g in genres.split(",") if g.strip()],
        "studio": studio.strip(),
        "tour": tour.strip(),
        "include": _parse_include(include),
        "limit_rate": rate,
        "live": _truthy(live),
        "direct": _truthy(direct),
        "clip": _parse_clip(clip),
        "extra_opts": _parse_extra_opts(ytdlp_opts),
        "page_capture": wants_page_capture,
    }
    return params, show_dir


@app.post("/jobs")
def create_job(
    url: str = Form(...),
    show: str = Form(...),
    season: str = Form(""),
    naming: str = Form("performance"),
    reverse: str = Form(""),
    quality: str = Form("max"),
    write_subs: str = Form(""),
    sponsor_marks: str = Form(""),
    cookies_browser: str = Form(""),
    destination: str = Form(""),
    run_rename: str = Form(""),
    run_nfo: str = Form(""),
    run_artwork: str = Form(""),
    genres: str = Form("Music, Concert"),
    studio: str = Form(""),
    tour: str = Form(""),
    include: str = Form(""),
    limit_rate: str = Form(""),
    live: str = Form(""),
    direct: str = Form(""),
    clip: str = Form(""),
    ytdlp_opts: str = Form(""),
    audio_only: str = Form(""),
    author: str = Form(""),
    page_capture: str = Form(""),
):
    _require_addon_on(naming)
    params, show_dir = _build_job_params(
        url=url, show=show, season=season, naming=naming, reverse=reverse,
        quality=quality, write_subs=write_subs, sponsor_marks=sponsor_marks,
        cookies_browser=cookies_browser,
        destination=destination, run_rename=run_rename, run_nfo=run_nfo,
        run_artwork=run_artwork, genres=genres, studio=studio, tour=tour, include=include,
        limit_rate=limit_rate, live=live, direct=direct, clip=clip, ytdlp_opts=ytdlp_opts,
        audio_only=audio_only, author=author, page_capture=page_capture)
    history.add(params["url"], params["show"])
    # start_new() decides whether this is large enough to auto-chunk (a
    # 100+ item job is one opaque 30-40 minute black box with no partial
    # feedback and a big blast radius on failure -- surfaced by a real
    # 132-item audiobook job) and creates+starts the right thing either way.
    # Shared with retry_job() so a retry of a large job gets the same
    # resilience a fresh submission would.
    ids = jobs.start_new(params, show_dir)
    return RedirectResponse(f"/jobs/{ids[0]}", status_code=303)


@app.post("/preview")
def preview_job(
    url: str = Form(...),
    show: str = Form(""),
    season: str = Form(""),
    naming: str = Form("performance"),
    reverse: str = Form(""),
    quality: str = Form("max"),
    write_subs: str = Form(""),
    sponsor_marks: str = Form(""),
    cookies_browser: str = Form(""),
    destination: str = Form(""),
    run_rename: str = Form(""),
    run_nfo: str = Form(""),
    run_artwork: str = Form(""),
    genres: str = Form("Music, Concert"),
    studio: str = Form(""),
    tour: str = Form(""),
    limit_rate: str = Form(""),
    clip: str = Form(""),
    ytdlp_opts: str = Form(""),
    audio_only: str = Form(""),
    author: str = Form(""),
    page_capture: str = Form(""),
):
    _require_addon_on(naming)
    params, show_dir = _build_job_params(
        url=url, show=show, season=season, naming=naming, reverse=reverse,
        quality=quality, write_subs=write_subs, sponsor_marks=sponsor_marks,
        cookies_browser=cookies_browser,
        destination=destination, run_rename=run_rename, run_nfo=run_nfo,
        run_artwork=run_artwork, genres=genres, studio=studio, tour=tour, limit_rate=limit_rate,
        clip=clip, ytdlp_opts=ytdlp_opts, require_show=False, audio_only=audio_only,
        author=author, page_capture=page_capture)
    if params["show"] != _UNTITLED:
        history.add(params["url"], params["show"])
    return preview.preview(params, show_dir)


@app.get("/api/history")
def api_history():
    return history.list_urls()


@app.get("/api/settings")
def api_settings():
    return {
        "destination": settings.destination(),
        "library_root": str(config.LIBRARY_ROOT),
        "media_folders": [e["name"] for e in library.browse("").get("entries", [])],
    }


@app.post("/settings")
def save_settings(destination: str = Form(...)):
    dest = config.clean_dest(destination)
    if not dest:
        raise HTTPException(400, "Destination cannot be empty.")
    try:
        target = config.resolve_dest_dir(dest)  # validates it stays in ALLOWED_ROOTS
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    target.mkdir(parents=True, exist_ok=True)  # so it shows up in the tree
    settings.save(destination=dest)
    return {"ok": True, "destination": dest}


@app.post("/settings/addons")
def save_addon(request: Request, name: str = Form(...), on: str = Form("")):
    """Switch an add-on preset (Audiobook / Podcast) on or off."""
    _require(request, "admin")
    if name not in presets.ADDON_NAMES:
        raise HTTPException(400, f"Unknown add-on: {name}")
    return {"ok": True, "addons": sorted(settings.set_addon(name, _truthy(on)))}


def _require_addon_on(naming: str) -> None:
    """New jobs/previews only. Retries go through jobs.retry_job(), which never
    calls this, so an old audiobook job can still be retried with it off."""
    if presets.is_addon(naming) and naming not in settings.addons_enabled():
        raise HTTPException(400, f"The {presets.label(naming)} add-on is switched off. "
                                 "Turn it on under Add-ons (top right) to use it.")


@app.post("/cleanup")
def cleanup(destination: str = Form(""), show: str = Form("")):
    """Remove orphaned partial-download files from a destination/show (and any
    leftover staging). For clearing junk from failed runs."""
    dest = config.clean_dest(destination) or settings.destination()
    show = show.strip()
    rel = f"{dest}/{show}" if show else dest
    try:
        res = library.sweep_partials(rel)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return res


@app.get("/api/browse")
def api_browse(path: str = ""):
    """List immediate subfolders (with counts) of LIBRARY_ROOT/path."""
    try:
        return library.browse(path)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/suggest-show")
def api_suggest_show(url: str = "", cookies_browser: str = ""):
    """Suggest a show/artist name from a URL's channel (for auto-fill)."""
    return preview.suggest_show(url, cookies_browser)


@app.get("/api/show-info")
def api_show_info(show: str, destination: str = "", naming: str = "", author: str = ""):
    """Existence + seasons present for a show under a destination.

    naming/author let this mirror _build_job_params's Author/Title nesting
    for audiobook -- otherwise this always checked the OLD flat path even
    after a job would actually write to the nested one.
    """
    dest = config.clean_dest(destination) or settings.destination()
    try:
        return library.show_info(dest, _folder_id(naming, show, author))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/active")
def api_active():
    return jobs.active_summary()


@app.get("/api/jobs")
def api_jobs():
    return jobs.list_jobs()


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    job["files"] = jobs.list_output_files(job_id)
    job["error_summary"] = jobs.error_summary(job_id)
    return job


@app.get("/jobs/{job_id}/log")
async def stream_log(job_id: str):
    if not jobs.get_job(job_id):
        raise HTTPException(404, "Job not found")
    log_path = config.LOGS_DIR / f"{job_id}.log"

    async def gen():
        pos = 0
        while True:
            if log_path.exists():
                with open(log_path, encoding="utf-8", errors="replace") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                    pos = fh.tell()
                for line in chunk.splitlines():
                    yield f"data: {line}\n\n"
            status = jobs.get_status(job_id)
            if status in jobs.TERMINAL:
                # one final read in case lines landed after the status flip
                if log_path.exists():
                    with open(log_path, encoding="utf-8", errors="replace") as fh:
                        fh.seek(pos)
                        for line in fh.read().splitlines():
                            yield f"data: {line}\n\n"
                yield f"event: end\ndata: {status}\n\n"
                return
            await asyncio.sleep(1.0)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    if not jobs.get_job(job_id):
        raise HTTPException(404, "Job not found")
    ok = jobs.cancel_job(job_id)
    return {"cancelled": ok}


@app.post("/jobs/{job_id}/retry")
def retry_job(job_id: str):
    """Start a fresh job with the same params. --download-archive already
    only records videos that fully completed, so this naturally re-attempts
    ONLY what failed last time -- see jobs.retry_job()."""
    new_id = jobs.retry_job(job_id)
    if not new_id:
        raise HTTPException(404, "Job not found")
    return RedirectResponse(f"/jobs/{new_id}", status_code=303)


@app.post("/update")
def update_now():
    return updater.update_ytdlp()


@app.get("/health")
def health():
    return JSONResponse({"ok": True, "yt_dlp": updater.ytdlp_version()})


# =============================================================================
# Concert queue — phase 1: sources + discovery.
#
# NO AUTH YET. These are LAN-only, exactly like the rest of the app, and must be
# gated behind Authentik before Encoreum is exposed on example.com (phase 4).
# Requesters must never reach the operator UI: it carries the Advanced yt-dlp
# passthrough, job cancellation and direct library writes.
# =============================================================================
@app.get("/api/queue/sources")
def api_queue_sources(request: Request):
    """Anyone who can browse sees the watch-list. Only admins see unapproved ones."""
    me = _require(request, "browse")
    rows = catalog.list_sources()
    if not me.is_admin:
        rows = [r for r in rows if r.get("approved")]
    return {"sources": rows}


@app.post("/api/queue/sources")
def api_queue_add_source(
    request: Request,
    channel: str = Form(...),
    show: str = Form(""),
    destination: str = Form("concerts"),
    ephemeral: str = Form("1"),
    auto_record: str = Form(""),
):
    """Register a channel to watch. Accepts a channel_id, @handle or URL.

    GATING (fixed 2026-08-21): these endpoints predated app/auth.py and were
    left ungated — a plain `friends` member could add a source, which is a
    write to what the server goes off and does unattended.

    Admins add directly. Anyone who can request may SUGGEST a channel: it lands
    with approved=0, is not polled, and is invisible to non-admins until an
    admin approves it. `sources.approved` already existed for exactly this and
    nothing had ever set it to 0.
    """
    me = _require(request, "request")
    res = discovery.add_channel(
        channel.strip(), show=show.strip(), destination=destination.strip() or "concerts",
        ephemeral=_truthy(ephemeral),
        # Only an admin may arm unattended recording at creation time.
        auto_record=_truthy(auto_record) and me.is_admin,
        added_by=me.username)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "could not add channel"))
    if not me.is_admin:
        catalog.set_source_approved(res["source_id"], False)
        res["approved"] = False
        res["note"] = "suggested — an admin must approve it before it is polled"
    else:
        res["approved"] = True
    return res


@app.post("/api/queue/sources/{source_id}/approve")
def api_queue_source_approve(source_id: str, request: Request,
                             approved: str = Form("1")):
    """Approve (or un-approve) a suggested channel. Admin only."""
    _require(request, "admin")
    if not catalog.get_source(source_id):
        raise HTTPException(404, "no such source")
    catalog.set_source_approved(source_id, _truthy(approved))
    return {"ok": True, "source": catalog.get_source(source_id)}


@app.delete("/api/queue/sources/{source_id}")
def api_queue_delete_source(source_id: str, request: Request):
    _require(request, "admin")
    if not catalog.get_source(source_id):
        raise HTTPException(404, "no such source")
    catalog.delete_source(source_id)
    return {"ok": True}


@app.get("/api/queue/upcoming")
def api_queue_upcoming():
    """What the browse page will render. Feed-derived, so it costs nothing."""
    return {"events": catalog.upcoming()}


@app.get("/api/queue/stats")
def api_queue_stats():
    """Counts plus poller health.

    `next_poll` is here deliberately: a discovery service whose timer silently
    failed to register looks identical to one with nothing to report. Surface
    it so "no upcoming concerts" can be told apart from "not actually polling".
    """
    out = catalog.stats()
    job = _scheduler.get_job("concert-queue-poll") if _scheduler.running else None
    out["poller"] = {
        "scheduled": bool(job),
        "next_poll": job.next_run_time.isoformat() if job and job.next_run_time else None,
        "interval_minutes": 30,
    }
    return out


@app.get("/api/queue/requests/count")
def api_queue_request_count():
    """Nav-badge aggregate, modelled on Seerr's /request/count."""
    return catalog.request_counts()


@app.post("/api/queue/poll")
def api_queue_poll():
    """Trigger a discovery pass now instead of waiting for the 30-min timer."""
    return discovery.poll_once()


# =============================================================================
# Concert queue — identity, the shared feed, and auto-approve.
#
# Identity comes from Authentik's forward-auth headers (see app/auth.py). This
# app implements no login of its own: the family signs in with Plex, Authentik
# brokers it, Traefik passes the result down. Measured 2026-08-21: the Plex
# source has 18 linked identities and the `friends` group has the same 18.
# =============================================================================
def _me(request: Request) -> auth.Identity:
    return auth.identify(request)


def _require(request: Request, what: str) -> auth.Identity:
    me = _me(request)
    ok = {"browse": me.can_browse, "request": me.can_request, "admin": me.is_admin}[what]
    if not ok:
        raise HTTPException(403, f"not permitted ({what}); you are '{me.role}'")
    return me


def _effective_auto_approve(me: auth.Identity) -> bool:
    """In-app override wins over the Authentik tier.

    So you can grant auto-approve to one person from inside this app without
    editing group membership in Authentik. NULL means inherit the tier.
    """
    pref = catalog.get_user_pref(me.username) or {}
    if pref.get("auto_approve") is not None:
        return bool(pref["auto_approve"])
    return me.auto_approved


@app.get("/api/queue/me")
def api_queue_me(request: Request):
    """Who the server thinks you are — the first thing to check when tiers misbehave."""
    me = _me(request)
    pref = catalog.get_user_pref(me.username) or {}
    return {
        "username": me.username, "display": me.display, "role": me.role,
        "groups": me.groups, "authenticated": me.authenticated,
        "can_browse": me.can_browse, "can_request": me.can_request,
        "is_admin": me.is_admin,
        "auto_approve": _effective_auto_approve(me),
        "auto_approve_source": "override" if pref.get("auto_approve") is not None else "group",
    }


@app.get("/api/queue/requests")
def api_queue_requests(request: Request):
    """The shared feed — everyone who can browse sees who asked for what.

    This is the Seerr behaviour: request activity is public to the household.
    It stops three people requesting the same concert, and it makes the library
    feel shared rather than administered.
    """
    _require(request, "browse")
    return {"requests": catalog.request_feed(), "counts": catalog.request_counts()}


@app.post("/api/queue/requests")
def api_queue_request_create(request: Request, event_id: str = Form(""),
                             segment_id: str = Form(""), note: str = Form("")):
    me = _require(request, "request")
    pref = catalog.get_user_pref(me.username) or {}
    if pref.get("blocked"):
        raise HTTPException(403, "your requests are currently blocked")

    # A request targets EITHER a whole broadcast or one set at a festival.
    seg = catalog.get_segment(segment_id) if segment_id else None
    if segment_id and not seg:
        raise HTTPException(404, "no such set")
    if seg and not event_id:
        # The stage's stream may not exist yet — the schedule is published days
        # before the broadcast. That is fine; the capture resolves it later.
        event_id = seg.get("event_id") or ""
    if not seg:
        if not event_id or not catalog.get_event(event_id):
            raise HTTPException(404, "no such event")

    auto = _effective_auto_approve(me)

    # Quota applies only to auto-approval. Hitting it does not reject the
    # request — it downgrades to needing approval, which is friendlier than
    # refusing outright and still keeps a ceiling on unattended recording.
    limit, days = pref.get("quota_limit"), pref.get("quota_days")
    quota_hit = False
    if auto and limit and days and catalog.requests_in_window(me.username, days) >= limit:
        auto, quota_hit = False, True

    status = catalog.REQ_APPROVED if auto else catalog.REQ_PENDING
    rid = catalog.add_request(event_id, me.username, note=note, status=status,
                              display=me.display)
    if seg:
        with catalog._db_lock, catalog._connect() as _c:
            _c.execute("UPDATE requests SET segment_id=? WHERE id=?", (segment_id, rid))
    if auto:
        catalog.set_request_status(rid, catalog.REQ_APPROVED, decided_by="auto")
    return {"ok": True, "request_id": rid, "status": status,
            "auto_approved": auto, "quota_reached": quota_hit}


@app.post("/api/queue/requests/{request_id}/decide")
def api_queue_request_decide(request_id: str, request: Request,
                             decision: str = Form(...)):
    me = _require(request, "admin")
    if decision not in (catalog.REQ_APPROVED, catalog.REQ_DECLINED):
        raise HTTPException(400, "decision must be 'approved' or 'declined'")
    catalog.set_request_status(request_id, decision, decided_by=me.username)
    return {"ok": True, "request_id": request_id, "status": decision,
            "decided_by": me.username}


@app.get("/api/queue/users")
def api_queue_users(request: Request):
    """Per-user overrides. Admin-only — it exposes everyone's settings."""
    _require(request, "admin")
    return {"users": catalog.list_user_prefs()}


@app.post("/api/queue/users/{username}")
def api_queue_user_set(username: str, request: Request,
                       auto_approve: str = Form(""), blocked: str = Form(""),
                       quota_limit: str = Form(""), quota_days: str = Form(""),
                       display: str = Form(""), note: str = Form("")):
    """Set auto-approve (and quota) from inside this app — no Authentik edit needed.

    auto_approve: "1"/"0" to force, "" to clear the override and inherit the tier.
    """
    me = _require(request, "admin")
    # "" from the form means CLEAR the override, not "leave unchanged".
    aa = catalog.INHERIT if auto_approve == "" else _truthy(auto_approve)
    bl = None if blocked == "" else _truthy(blocked)
    ql = int(quota_limit) if quota_limit.strip().isdigit() else None
    qd = int(quota_days) if quota_days.strip().isdigit() else None
    catalog.set_user_pref(username, display=display, auto_approve=aa, blocked=bl,
                          quota_limit=ql, quota_days=qd,
                          note=note or None, updated_by=me.username)
    return {"ok": True, "user": catalog.get_user_pref(username)}


# =============================================================================
# Festivals -> stages -> sets.
#
# A festival is not one stream: Coachella runs seven stages as seven separate
# YouTube streams, each carrying many acts. So people pick SETS, not weekends —
# a selected set is one stage's stream URL plus a start and an end.
# =============================================================================
@app.get("/api/queue/festivals")
def api_festivals(request: Request):
    _require(request, "browse")
    return {"festivals": catalog.list_festivals()}


@app.post("/api/queue/festivals")
def api_festival_create(request: Request, name: str = Form(...),
                        year: int = Form(...), show: str = Form(""),
                        timezone_name: str = Form("America/Los_Angeles"),
                        destination: str = Form("concerts"), notes: str = Form("")):
    _require(request, "admin")
    fid = catalog.add_festival(name.strip(), year, show.strip(), timezone_name.strip(),
                               destination.strip() or "concerts", notes.strip())
    if not fid:
        raise HTTPException(400, "could not create festival")
    return {"ok": True, "festival": catalog.get_festival(fid)}


@app.post("/api/queue/festivals/{festival_id}/stages")
def api_stage_add(festival_id: str, request: Request, name: str = Form(...),
                  stream_url: str = Form(""), event_id: str = Form("")):
    _require(request, "admin")
    if not catalog.get_festival(festival_id):
        raise HTTPException(404, "no such festival")
    sid = catalog.add_stage(festival_id, name.strip(), stream_url.strip(), event_id.strip())
    return {"ok": True, "stage_id": sid, "stages": catalog.list_stages(festival_id)}


@app.post("/api/queue/stages/{stage_id}/schedule")
def api_schedule_ingest(stage_id: str, request: Request, text: str = Form(...),
                        day: str = Form(...), dry_run: str = Form("1"),
                        default_minutes: int = Form(50)):
    """Paste a stage's set times. Defaults to a DRY RUN.

    Dry run by default on purpose: a mis-parsed schedule records the wrong hour,
    and the file looks fine until someone plays it. Check the parse, then commit.

    !! Use the STREAMING schedule, not the on-site lineup !! Verified for
    Coachella: some sets air delayed — a 3pm on-site set was not viewable until
    the 4pm streams began.
    """
    _require(request, "admin")
    st = next((s for s in sum([catalog.list_stages(f["id"])
                               for f in catalog.list_festivals()], [])
               if s["id"] == stage_id), None)
    if not st:
        raise HTTPException(404, "no such stage")
    fest = catalog.get_festival(st["festival_id"])
    segs, bad = schedule.build_segments(text, day.strip(), fest["timezone"],
                                        default_minutes)
    if _truthy(dry_run):
        return {"dry_run": True, "stage": st["name"], "festival": fest["name"],
                "timezone": fest["timezone"], "parsed": len(segs),
                "unparsed": bad, "segments": segs}
    created = [catalog.add_segment(stage_id, s["artist"], s["starts_at"], s["ends_at"],
                                   raw_time=s["raw_time"], schedule_src="pasted")
               for s in segs]
    return {"dry_run": False, "created": len([c for c in created if c]),
            "unparsed": bad}


@app.get("/api/queue/schedule")
def api_schedule(request: Request, festival_id: str = "", upcoming: str = ""):
    """The set list people choose from — ordered by start time, with genre and
    what the library already holds for each artist."""
    _require(request, "browse")
    return {"sets": catalog.festival_schedule(festival_id, _truthy(upcoming))}


@app.post("/api/queue/enrich")
def api_enrich(request: Request, budget: int = Form(20)):
    """Resolve artists for newly ingested sets now, rather than at the next poll."""
    _require(request, "admin")
    return discovery.enrich_segments(budget=budget)


@app.get("/api/queue/integrations")
def api_integrations(request: Request):
    """Is each integration configured, and does it actually work?

    Deliberately available to any browser, not admin-only: when the set list
    says "could not identify this artist", ANYONE looking at it should be able
    to see whether that is a missing key rather than an unknown band.
    """
    _require(request, "browse")
    return discovery.check_integrations()


# =============================================================================
# Plex library integration — where things go, and what has been watched.
# =============================================================================
@app.get("/api/queue/plex/libraries")
def api_plex_libraries(request: Request):
    """Every Plex library, with its path translated into THIS container's view.

    Plex reports its own bind-mount paths, which do not exist here — the same
    folder is /data/media1/concerts to Plex and /library/concerts to us. The
    mapping is resolved by testing which suffix actually exists on disk.
    """
    _require(request, "browse")
    return {"libraries": plexlib.sections()}


@app.get("/api/queue/plex/targets")
def api_plex_targets(request: Request):
    """Libraries this app could write concerts into, ranked, with what is wrong.

    A library whose agent is 'none' will show every file with no metadata at
    all, which reads as the app being broken. Better to say so up front than to
    let someone pick it and wonder.
    """
    _require(request, "browse")
    return {"targets": plexlib.target_libraries()}


@app.get("/api/queue/plex/unwatched")
def api_plex_unwatched(request: Request, section: str, days: int = 180):
    """Recorded, never played, older than N days — the retention question."""
    _require(request, "admin")
    items = plexlib.unwatched_older_than(section, days)
    return {"section": section, "days": days, "count": len(items), "items": items}


# =============================================================================
# Ingest review queue.
#
# Manual uploads NEVER reach the library unreviewed. Everything up to the
# decision is automatic — scan, unpack, parse, align, propose — so review is one
# yes/no and not data entry. See app/imports.py for why origin, and not
# confidence, is what gates an unattended write.
# =============================================================================
@app.get("/review", response_class=HTMLResponse)
def review_page(request: Request):
    """One page for both review queues.

    They were API-only until now, which made them theatre: a review queue nobody
    can see does not get reviewed, and the uploads queue in particular is the
    thing standing between a hand-dropped archive and the library.
    """
    _require(request, "admin")
    # Newer Starlette signature: (request, name, context). The legacy
    # (name, {"request": ...}) form fails with "unhashable type: dict" here.
    return templates.TemplateResponse(request, "review.html", {})


# --- sponsor segments (SponsorBlock: external, free, opt-in; see sponsorblock.py)

@app.get("/sponsors", response_class=HTMLResponse)
def sponsors_page(request: Request):
    """Scan the library for SponsorBlock sponsor segments; add/undo chapter marks."""
    _require(request, "admin")
    return templates.TemplateResponse(request, "sponsors.html", {
        "destination": config.DEFAULT_DEST,
        "attribution": sponsorblock.ATTRIBUTION})


@app.get("/api/sponsors/check")
def api_sponsors_check(ids: str = ""):
    """Preview-time lookup for a page of video ids. Read-only; same access as
    /preview. Capped so one call can't spend a meaningful share of the budget."""
    vids = [v for v in ids.split(",") if _re.fullmatch(r"[A-Za-z0-9_-]{6,20}", v)][:60]
    return sponsorblock.lookup(vids)


@app.post("/api/sponsors/scan")
def api_sponsors_scan(request: Request, destination: str = Form("")):
    _require(request, "admin")
    try:
        return sponsorblock.start_scan(destination)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/sponsors/scan")
def api_sponsors_scan_status(request: Request):
    _require(request, "admin")
    return sponsorblock.scan_status()


@app.post("/api/sponsors/apply")
def api_sponsors_apply(request: Request, path: str = Form(...)):
    """Add chapter marks to ONE library file (stream copy, nothing cut, original
    chapters backed up). The page calls this once per file so it can show
    progress; each call takes about as long as copying the file once."""
    _require(request, "admin")
    try:
        return sponsorblock.apply_marks(path)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "message": str(e)}


@app.post("/api/sponsors/undo")
def api_sponsors_undo(request: Request, path: str = Form(...)):
    _require(request, "admin")
    try:
        return sponsorblock.undo_marks(path)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "message": str(e)}


@app.get("/api/imports")
def api_imports(request: Request, status: str = "", limit: int = 100):
    _require(request, "admin")
    return {"pending": imports.count_pending(),
            "imports": imports.listing(status, limit),
            "ingest_dir": str(imports.INGEST_DIR),
            "library": str(imports.LIVE_AUDIO_DIR)}


@app.get("/api/imports/{iid}")
def api_import_get(iid: str, request: Request):
    _require(request, "admin")
    d = imports.get(iid)
    if not d:
        raise HTTPException(404, "no such import")
    return d


@app.post("/api/imports/scan")
def api_imports_scan(request: Request):
    """Look for new drops now. Idempotent; never writes to the library."""
    _require(request, "admin")
    return imports.scan()


@app.post("/api/imports/{iid}/fix")
def api_import_fix(iid: str, request: Request, artist: str = Form(None),
                   date: str = Form(None), venue: str = Form(None),
                   city: str = Form(None), source_type: str = Form(None),
                   setlist: str = Form(None), cross_link: str = Form(None),
                   note: str = Form(None)):
    """Fix a bad match. Re-aligns immediately so the correction is visible
    BEFORE anything is written. Send a field as "" to clear it and fall back to
    what the parser proposes."""
    me = _require(request, "admin")
    r = imports.set_overrides(iid, by=me.username, artist=artist, date=date,
                              venue=venue, city=city, source_type=source_type,
                              setlist=setlist, cross_link=cross_link, note=note)
    if not r["ok"]:
        raise HTTPException(400, r["error"])
    return r


@app.post("/api/imports/{iid}/approve")
def api_import_approve(iid: str, request: Request, force: str = Form("")):
    """Write it to the library. This is the only path that does."""
    me = _require(request, "admin")
    r = imports.approve(iid, by=me.username, force=_truthy(force))
    if not r["ok"]:
        raise HTTPException(400, r["error"])
    return r


@app.post("/api/imports/{iid}/reject")
def api_import_reject(iid: str, request: Request, reason: str = Form("")):
    me = _require(request, "admin")
    r = imports.reject(iid, by=me.username, reason=reason.strip())
    if not r["ok"]:
        raise HTTPException(400, r["error"])
    return r


# =============================================================================
# Captures: metadata in two stages.
#
# Stage 1 is written when the recording finishes, from what the app already
# knew. Stage 2 attaches a setlist if one is ever posted. `minimal` is a
# COMPLETE state, not a failure — most captures stay there and that is expected.
# =============================================================================
@app.get("/api/captures")
def api_captures(request: Request, state: str = "", limit: int = 200):
    _require(request, "browse")
    return {"stats": captures.stats(), "captures": captures.listing(state, limit)}


# Declared BEFORE /{cid}: FastAPI matches in declaration order, so a literal
# path that comes after a parameterised one is unreachable — this route would
# have been swallowed as cid="unmatched" and 404'd.
@app.get("/api/captures/unmatched")
def api_captures_unmatched(request: Request, limit: int = 500):
    """Every download with no confirmed setlist — the mark, as a list.

    Covers `minimal` (nothing found yet) AND `review` (something found that
    nobody has confirmed): from the library's point of view both mean the same
    thing, which is that this recording has no setlist attached.
    """
    _require(request, "browse")
    items = captures.unmatched(limit)
    return {"count": len(items), "stats": captures.stats(), "captures": items}


@app.get("/api/captures/{cid}")
def api_capture_get(cid: str, request: Request):
    _require(request, "browse")
    c = captures.get(cid)
    if not c:
        raise HTTPException(404, "no such capture")
    return c


@app.post("/api/captures/{cid}/enrich")
def api_capture_enrich(cid: str, request: Request):
    """Ask setlist.fm now instead of waiting for the schedule."""
    _require(request, "admin")
    r = captures.enrich_one(cid)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "lookup failed"))
    return r


@app.post("/api/captures/{cid}/setlist")
def api_capture_setlist(cid: str, request: Request, text: str = Form(...),
                        url: str = Form("")):
    """Paste a setlist by hand. Works with or without an API key, and marks the
    capture `manual` so no later lookup can overwrite it."""
    me = _require(request, "admin")
    r = captures.attach_pasted(cid, text, by=me.username, url=url.strip())
    if not r["ok"]:
        raise HTTPException(400, r["error"])
    return r


@app.post("/api/captures/{cid}/fix")
def api_capture_fix(cid: str, request: Request, artist: str = Form(None),
                    festival: str = Form(None), stage: str = Form(None),
                    venue: str = Form(None), city: str = Form(None),
                    tour: str = Form(None),
                    performance_date: str = Form(None)):
    """Correct a capture. Corrections outrank both metadata stages permanently.
    Correcting the artist or the date REOPENS a lookup that had given up."""
    me = _require(request, "admin")
    r = captures.set_overrides(cid, by=me.username, artist=artist,
                              festival=festival, stage=stage, venue=venue,
                              city=city, tour=tour,
                              performance_date=performance_date)
    if not r["ok"]:
        raise HTTPException(400, r["error"])
    return r


@app.post("/api/captures/{cid}/confirm")
def api_capture_confirm(cid: str, request: Request):
    """Accept a proposed setlist that was not certain enough to auto-apply."""
    me = _require(request, "admin")
    r = captures.confirm(cid, by=me.username)
    if not r["ok"]:
        raise HTTPException(400, r["error"])
    return r


@app.post("/api/captures/{cid}/decline")
def api_capture_decline(cid: str, request: Request, reason: str = Form("")):
    """Reject a proposed setlist and keep looking. The declined setlist is
    remembered so it is not re-proposed on every later pass."""
    me = _require(request, "admin")
    r = captures.decline(cid, by=me.username, reason=reason.strip())
    if not r["ok"]:
        raise HTTPException(400, r["error"])
    return r


@app.post("/api/captures/enrich")
def api_captures_enrich_all(request: Request, limit: int = 20):
    _require(request, "admin")
    return captures.enrich_pass(limit)


@app.get("/api/credentials")
def api_credentials(request: Request):
    """Where each credential comes from. NEVER returns a value.

    `secure` is false for anything still read from an environment variable —
    which is visible in `docker inspect` and inherited by every child process,
    and this app spawns yt-dlp, streamlink and ffmpeg.
    """
    _require(request, "admin")
    return {"credentials": secrets.audit()}


@app.get("/api/ratelimits")
def api_ratelimits(request: Request):
    """Every external service's limits, what they cost us, and where each number
    came from — `source` says whether a figure was PUBLISHED by the vendor or
    chosen by us. Budgets are rolling 24h and persist across restarts."""
    _require(request, "browse")
    return {"limits": ratelimit.all_status()}


@app.get("/api/setlistfm/status")
def api_setlistfm_status(request: Request):
    """Which mode setlist lookup is in. Deliberately explicit: with no key the
    app is not degraded, it is in manual mode, and that should be visible."""
    _require(request, "browse")
    return setlistfm.available()
