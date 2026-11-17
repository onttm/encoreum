"""
pipeline.py -- the whole reason this app exists.

Builds the yt-dlp command (ported verbatim from example-tour-download.sh) and
runs the three-step job: download -> (optional) footage-date rename -> (optional)
NFO generation. All three naming modes are supported, including ``episode`` mode
(the reverse-ordered per-video loop from the bash script).

Every child process is launched through the JobControl passed in, so a job can
be cancelled by killing the process group (see jobs.py).
"""
import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from . import config, library, presets


def _clean_title(t: str) -> str:
    t = re.sub(r'[\\/:*?"<>|]', " ", t or "").strip()
    return re.sub(r"\s+", " ", t)[:150].rstrip(" .")

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

# --- filename-only shortening for audio presets ------------------------------
# The book/show Title is already the FOLDER name (Author/Title/ or dest/Show/)
# -- repeating it in full on every single track's filename is pure redundancy,
# and for a long title (a real one: "New Tales of the Example Mythos") that
# repetition is what makes each filename needlessly long. Neither of these
# ever touches the folder name or embedded metadata (--embed-metadata already
# writes the real full title) -- strictly a per-track FILENAME concern.
SHOW_ACRONYM_THRESHOLD = 40   # chars; below this the real title is left alone
CHAPTER_TITLE_MAX = 80        # chars; enforced by yt-dlp's own %(title).<N>s

_ACRONYM_STOPWORDS = {"a", "an", "and", "as", "at", "but", "by", "for", "from",
                      "in", "of", "on", "or", "the", "to", "with"}


def short_title(title: str, threshold: int = SHOW_ACRONYM_THRESHOLD) -> str:
    """Acronym-ize a long title for use ONLY as a per-track filename prefix.
    Short titles pass through untouched. Skips common short stopwords so the
    result reads closer to real initials ("New Tales of the Example Mythos"
    -> "NTCM", not "NTOTCM"). Reused by preview.py so the dry run matches the
    real download exactly -- see pipeline.flat_audio_template."""
    title = (title or "").strip()
    if len(title) <= threshold:
        return title
    words = [w for w in re.findall(r"[A-Za-z0-9]+", title) if w.lower() not in _ACRONYM_STOPWORDS]
    acronym = "".join(w[0].upper() for w in words if w)
    return acronym or title[:threshold]


class Cancelled(Exception):
    """Raised when a job is cancelled mid-run."""


class JobControl:
    """
    Owns the currently-running child process for one job and provides a
    thread-safe cancel that terminates the whole process group.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self.cancelled = False
        # Set on cancel so a waiting job wakes immediately. Without this, a job
        # parked on a scheduled stream (potentially for days) would ignore the
        # Cancel button until its next poll — cancel only ever killed a RUNNING
        # child process, and a sleeping job has none.
        self._wake = threading.Event()
        # Working directory for every child process. Without one, children
        # inherit the container's WORKDIR (/app, root-owned), so any yt-dlp
        # output that isn't covered by our -o template (e.g. --split-chapters'
        # chapter files) fails "Permission denied" -- a 27-show job hit that on
        # every item (2026-11-16). run_job() points this at a per-job scratch
        # dir under STAGING_DIR: writable, never published, wiped on startup.
        self.cwd = None

    def sleep(self, seconds: float) -> None:
        """Cancellable sleep. Raises Cancelled the moment cancel() is called."""
        if self._wake.wait(seconds):
            raise Cancelled()
        if self.cancelled:
            raise Cancelled()

    def run(self, cmd, log, capture=False):
        """
        Start ``cmd``, stream (or capture) its output, wait for it, return
        (returncode, captured_stdout_or_None). Honours cancellation.
        """
        with self._lock:
            if self.cancelled:
                raise Cancelled()
            self._proc = subprocess.Popen(
                cmd,
                cwd=self.cwd if self.cwd and os.path.isdir(self.cwd) else None,
                stdout=subprocess.PIPE if capture else log.file,
                stderr=subprocess.PIPE if capture else subprocess.STDOUT,
                start_new_session=True,  # own process group -> killpg on cancel
                text=True,
            )
        proc = self._proc
        out = None
        if capture:
            out, _ = proc.communicate()
        else:
            proc.wait()
        with self._lock:
            self._proc = None
        if self.cancelled:
            raise Cancelled()
        return proc.returncode, out

    def cancel(self):
        with self._lock:
            self.cancelled = True
            proc = self._proc
        self._wake.set()          # release any job parked on a scheduled stream
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass


# --- output-filename templates (single source of truth: preview reuses these) ---
def date_template(show: str, show_dir, season: str) -> str:
    return f"{show_dir}/Season {season}/{show} - %(upload_date>%Y-%m-%d)s - %(title)s.%(ext)s"


def datecode_template(show: str, show_dir) -> str:
    return (f"{show_dir}/Season %(release_date,upload_date>%Y)s/"
            f"{show} - S%(release_date,upload_date>%Y)sE%(release_date,upload_date>%m%d)s"
            f" - %(title)s.%(ext)s")


def episode_template(show: str, season_dir, season: str, ep_str: str, title: str = "%(title)s") -> str:
    return f"{season_dir}/{show} - S{season}{ep_str} - {title}.%(ext)s"


def flat_audio_template(show: str, show_dir) -> str:
    """Flat folder, numbered by TRUE playlist position (stable even if a subset
    of items is selected) -- shared by every audio preset (audiobook/podcast/
    music): e.g. audiobooks/<Show>/<Show> - 003 - Chapter 3.m4a.

    The Show prefix is acronym-ized when long (short_title) -- it's already
    the folder name, so the full form costs nothing to keep there. The
    per-track title is truncated by yt-dlp itself (%(title).<N>s) since that
    field isn't known until yt-dlp resolves each video, unlike `show`."""
    prefix = short_title(show)
    return f"{show_dir}/{prefix} - %(playlist_index)03d - %(title).{CHAPTER_TITLE_MAX}s.%(ext)s"


# --- continuing an existing serial's numbering -------------------------------
# A serial audiobook/podcast gets new installments over time. Point a job at
# the SAME existing Author/Title folder for a fresh batch and, without this,
# yt-dlp numbers the new batch 001, 002... from scratch -- colliding with (or
# getting silently skipped by --no-overwrites next to) whatever's already
# there, instead of continuing 051, 052... The NNN is always exactly 3 digits
# flanked by " - " on both sides (flat_audio_template's own format), so it's
# a reliable anchor even though the Title itself may legitimately contain a
# "-" (e.g. "Dune - Book One").
_SEQ_NUM_RE = re.compile(r" - (\d{3}) - ")


_YEAR_DIR = re.compile(r"^(\d{4}|undated)$")


def existing_layout(show_dir: Path) -> str | None:
    """How a show folder is ALREADY laid out, from the media files in it.

    "season": files in "Season N" folders (datecode / date / episode /
              sequence modes).
    "year":   files in bare year folders (performance mode).
    "mixed":  both. None: no media yet (any mode is fine).

    Exists because switching modes on a populated show reorganises every file
    in it: a performance-mode job moved 224 existing Band A videos out of
    their Season folders on 2026-11-16 (reverted from the job log). CLAUDE.md
    already said "do not mix modes within one show"; this enforces it.
    """
    if not show_dir.is_dir():
        return None
    kinds = set()
    for d in show_dir.iterdir():
        if not d.is_dir():
            continue
        kind = "season" if d.name.startswith("Season ") else ("year" if _YEAR_DIR.match(d.name) else None)
        if kind and any(f.suffix.lower() in presets.MEDIA_EXTS for f in d.iterdir() if f.is_file()):
            kinds.add(kind)
    return "mixed" if len(kinds) > 1 else (kinds.pop() if kinds else None)


def layout_conflict(naming: str, run_rename: bool, layout: str | None) -> str:
    """A reason to SKIP the reorganising post-step, or "" if it is safe."""
    wants = ("year" if naming == "performance"
             else "season" if (naming == "sequence" or run_rename) else None)
    if not wants or not layout or layout == wants:
        return ""
    have = {"season": "Season folders (a date / datecode / episode / sequence mode)",
            "year": "year folders (performance mode)",
            "mixed": "a mix of Season folders and year folders"}[layout]
    mode = "performance" if wants == "year" else ("sequence" if naming == "sequence" else "rename-by-date")
    return (f"This show is already organised in {have}. Running {mode} here would "
            f"move EVERY existing episode into a new layout, so that step was skipped. "
            f"New downloads were added in the show's existing layout. To use one "
            f"scheme, pick the naming mode that matches the show; to convert the whole "
            f"show on purpose, run the script by hand on the folder (dry-run first).")


def existing_max_seq(show_dir: Path) -> int:
    """Highest NNN already present among audio-preset files directly in
    show_dir (0 if none/empty/doesn't exist yet). Audio presets are always a
    flat folder (no subfolders), so this only ever looks at show_dir itself."""
    best = 0
    try:
        if not show_dir.is_dir():
            return 0
        for f in show_dir.iterdir():
            if not f.is_file():
                continue
            m = _SEQ_NUM_RE.search(f.name)
            if m:
                best = max(best, int(m.group(1)))
    except OSError:
        pass
    return best


def continue_sequence(published: list, existing_max: int, log) -> None:
    """Shift THIS job's newly-published files' NNN past existing_max, so a new
    batch for an existing serial continues the sequence instead of restarting
    at 001. Only renumbers when there'd actually be a collision: if the new
    batch's own lowest number is already past existing_max (e.g. a re-run of
    the SAME growing playlist, where playlist_index already reflects the
    item's true position), it's left alone -- shifting it again would double-
    count. Only ever touches paths the caller just published itself."""
    if existing_max <= 0 or not published:
        return
    nums = {}
    for f in published:
        m = _SEQ_NUM_RE.search(f.name)
        if m:
            nums[f] = int(m.group(1))
    if not nums or min(nums.values()) > existing_max:
        return  # already past existing content -- nothing to shift
    log("")
    log(f"Continuing existing sequence (folder already has up to {existing_max:03d}).")
    for f, old_num in nums.items():
        new_num = old_num + existing_max
        m = _SEQ_NUM_RE.search(f.name)
        new_name = f.name[:m.start(1)] + f"{new_num:03d}" + f.name[m.end(1):]
        target = f.parent / new_name
        if target.exists():
            log(f"  ! skip (target already exists): {new_name}")
            continue
        try:
            f.rename(target)
            log(f"  {f.name} -> {new_name}")
        except OSError as e:
            log(f"  ! could not rename {f.name}: {e}")


def _playlist_items_args(p: dict) -> list:
    """--playlist-items for a dry-run selection (date/datecode modes)."""
    inc = p.get("include")
    return ["--playlist-items", ",".join(str(i) for i in inc)] if inc else []


def _common_opts(p: dict, archive: Path, audio_only: bool = False) -> list:
    """Shared yt-dlp options -- a direct port of COMMON_OPTS in the bash script.

    audio_only (any preset in presets.AUDIO_NAMES) swaps the video format-selection/merge flags
    for an audio-only extraction. It deliberately does NOT force a re-encode
    (`--audio-format best`, not `mp3`/`m4a`): per the static-image-audio-rip
    philosophy elsewhere in this app, lossy stays lossy -- transcoding an
    Opus/AAC source recovers nothing and only degrades it.
    """
    if audio_only:
        opts = [
            config.YTDLP_BIN,
            "-f", "bestaudio/best",
            # YouTube increasingly requires a PO Token for the default
            # client's audio-only formats; without one they LOOK available
            # (format selection succeeds) but 403 at actual download time, so
            # yt-dlp's own bestaudio/best fallback never kicks in -- the
            # failure happens after a format is already committed to.
            # Confirmed 2026-09-29 against a real 132-item playlist that
            # failed EVERY single item this way (0 of 132 audio files, only
            # thumbnail/info.json sidecars published). The android client
            # doesn't offer the gated format at all, so it lands on a
            # combined video+audio format instead -- costs bandwidth (the
            # video track downloads and is then discarded by -x), but
            # verified end-to-end: a real ~35-minute file downloaded clean
            # with no 403. Scoped to audio_only only -- the video path has
            # its own separately-tuned client/format resilience (see the
            # historical 403-throttling notes elsewhere in this file) that
            # this must not disturb.
            "--extractor-args", "youtube:player_client=android",
            "-x", "--audio-format", "best",
            "--embed-metadata", "--embed-thumbnail",
            "--write-thumbnail", "--convert-thumbnails", "jpg",
            "--write-info-json", "--no-write-playlist-metafiles",
            "--download-archive", str(archive),
            "--ignore-errors", "--no-overwrites",
            "--concurrent-fragments", "4",
            "--sleep-requests", "1.5",
            "--sleep-interval", "5",
            "--max-sleep-interval", "15",
            "--retries", "10",
            "--fragment-retries", "10",
            "--retry-sleep", "5",
            "--throttled-rate", "100K",
        ]
    else:
        fmt_sort = (
            "res:1080,vcodec:h264,acodec:aac,fps"
            if p.get("quality") == "compatible"
            else "res,fps,hdr:12,vcodec,channels,acodec"
        )
        opts = [
            config.YTDLP_BIN,
            "-f", "bv*+ba/b",
            "-S", fmt_sort,
            "--merge-output-format", "mkv",
            "--embed-metadata", "--embed-chapters", "--embed-thumbnail",
            "--write-thumbnail", "--convert-thumbnails", "jpg",
            "--write-info-json", "--no-write-playlist-metafiles",
            "--download-archive", str(archive),
            "--ignore-errors", "--no-overwrites",
            "--concurrent-fragments", "4",
            "--sleep-requests", "1.5",
            "--sleep-interval", "5",
            "--max-sleep-interval", "15",
            # --- resilience against YouTube throttling / intermittent HTTP 403 ---
            "--retries", "10",
            "--fragment-retries", "10",
            "--retry-sleep", "5",
            # if the transfer collapses (YT throttling), re-extract fresh URLs instead
            # of dying with a 403 partway through:
            "--throttled-rate", "100K",
        ]
    # Optional steady speed cap: bursting hard is what trips YouTube's 403; a
    # capped rate stays under the threshold (user-set, e.g. "4M").
    if p.get("limit_rate"):
        opts += ["--limit-rate", p["limit_rate"]]
    # Clip range → download only a section (needs ffmpeg; applies per video).
    if p.get("clip"):
        opts += ["--download-sections", p["clip"]]
    # Advanced passthrough: arbitrary (denylist-filtered) yt-dlp flags.
    if p.get("extra_opts"):
        opts += list(p["extra_opts"])
    if p.get("sponsor_marks"):
        # MARK, never remove: adds "[SponsorBlock]: Sponsor" chapters and leaves
        # the recording whole. Only sponsor/selfpromo -- intro/outro/filler/
        # music_offtopic get applied to walk-ons, banter and encore breaks on
        # concerts. Verified 2026-11-16 on real videos: an ad inside a chapter
        # splits it (X -> Sponsor -> X); a chapterless video gets title ->
        # Sponsor -> title. Needs --embed-chapters (always on above).
        opts += ["--sponsorblock-mark", "sponsor,selfpromo"]
    if p.get("write_subs"):
        opts += ["--write-subs", "--write-auto-subs", "--sub-langs", "en.*",
                 "--convert-subs", "srt", "--embed-subs"]
    if p.get("cookies_browser"):
        opts += ["--cookies-from-browser", p["cookies_browser"]]
    return opts


def _download(p: dict, show_dir: Path, control: JobControl, log) -> int:
    """Run the download step for the chosen naming mode. Returns a returncode."""
    show = p["show"]
    archive = config.ARCHIVES_DIR / f"{show}.txt"
    archive.parent.mkdir(parents=True, exist_ok=True)
    if p.get("page_capture"):
        return _download_page_capture(p, show_dir, control, log)
    naming = p.get("naming", "datecode")
    # Two different reasons a download can be audio-only: the naming mode IS
    # an audio preset (its own destination/organization, see presets.py), or
    # the plain "Audio only" checkbox was set on an otherwise-video mode (keep
    # that mode's naming/dating logic, just extract audio instead of video).
    common = _common_opts(p, archive, audio_only=(naming in presets.AUDIO_NAMES or p.get("audio_only")))
    url = p["url"]

    items = _playlist_items_args(p)
    if items:
        log(f"Selection: downloading playlist items {items[1]}")

    if naming in presets.AUDIO_NAMES:
        return _download_flat_audio(p, show_dir, common, items, control, log)

    if naming == "date":
        season = p.get("season") or "1"
        out = date_template(show, show_dir, season)
        cmd = common + items + ["-o", out, url]
        log(f"$ {shlex.join(cmd)}")
        rc, _ = control.run(cmd, log)
        return rc

    if naming == "datecode":
        out = datecode_template(show, show_dir)
        cmd = common + items + ["-o", out, url]
        log(f"$ {shlex.join(cmd)}")
        rc, _ = control.run(cmd, log)
        log("")
        log("NOTE: datecode names use the UPLOAD/RELEASE date. If that is not the")
        log("true concert date, enable 'Rename by footage date' on the job.")
        return rc

    if naming in ("sequence", "performance"):
        # Download to scratch (datecode by upload date); the post-step re-dates by
        # the concert date and lays the files out for real (sequence -> collision-
        # free S<year>E<nn>; performance -> 'YYYY.MM.DD - tour - venue - NNN').
        out = datecode_template(show, show_dir)
        cmd = common + items + ["-o", out, url]
        log(f"$ {shlex.join(cmd)}")
        rc, _ = control.run(cmd, log)
        return rc

    if naming == "episode":
        return _download_episode_mode(p, show_dir, common, control, log)

    raise ValueError(f"Unknown naming mode: {naming!r}")


def _download_episode_mode(p, show_dir, common, control, log) -> int:
    """
    Port of the episode-mode loop from example-tour-download.sh:
    read the playlist ids (flat), optionally reverse so the OLDEST becomes E001,
    then download each id with a fixed S<season>E<nnn> number.
    """
    show = p["show"]
    season = str(p.get("season") or "1")
    dest = show_dir / f"Season {season}"
    dest.mkdir(parents=True, exist_ok=True)

    log("Reading playlist order...")
    rc, out = control.run(
        [config.YTDLP_BIN, "--flat-playlist", "--print", "%(playlist_index)s\x1f%(id)s", p["url"]],
        log, capture=True,
    )
    rows = [ln.split("\x1f", 1) for ln in (out or "").splitlines() if ln.strip()]
    total = len(rows)
    log(f"Found {total} video(s).")
    if total == 0:
        log("ERROR: got 0 videos - check the URL/network.")
        return rc or 1

    ordered = list(reversed(rows)) if p.get("reverse", True) else rows
    include = set(p.get("include") or [])
    if include:
        log(f"Selection: {len(include)} of {total} chosen; episode numbers keep playlist position.")

    worst_rc = 0
    for n, row in enumerate(ordered, start=1):
        pidx = int(row[0]) if row[0].strip().isdigit() else n
        vid = row[1] if len(row) > 1 else row[0]
        if include and pidx not in include:
            continue
        ep = f"E{n:03d}"
        log("")
        log(f">>> S{season}{ep}  (video {n} of {total})")
        out_tmpl = episode_template(show, dest, season, ep)
        cmd = common + ["-o", out_tmpl, f"https://www.youtube.com/watch?v={vid}"]
        log(f"$ {shlex.join(cmd)}")
        rc, _ = control.run(cmd, log)
        if rc != 0:
            log(f"!!! Skipped S{season}{ep} (video unavailable) - number left as a gap.")
            worst_rc = worst_rc or rc
    return worst_rc


def _download_flat_audio(p: dict, show_dir: Path, common: list, items: list,
                         control: JobControl, log) -> int:
    """
    Any audio preset (audiobook/podcast/music): audio-only extraction, one
    flat folder per show. No reverse/renumber pass: each file keeps its TRUE
    playlist position (flat_audio_template's %(playlist_index)03d), so a
    track's number never changes even if only some items are selected --
    unlike concert modes, there is no per-video date to sort by, so playlist
    order (as the uploader made it) IS the order.
    """
    out = flat_audio_template(p["show"], show_dir)
    cmd = common + items + ["-o", out, p["url"]]
    log(f"$ {shlex.join(cmd)}")
    rc, _ = control.run(cmd, log)
    return rc


# --- page audio capture (scripts/capture-page-audio.py) -----------------------
# For web players yt-dlp has no extractor for. Same flat layout, numbering and
# staging as every other audio preset; only the source of the bytes differs.
_CAPTURE_MANIFEST = "page-capture.jsonl"


def page_capture_unavailable() -> str:
    """Why this install can't capture ('' if it can). Checked up front so a
    missing browser is a clear message at submit time, not a failed job."""
    if importlib.util.find_spec("playwright") is None:
        return "Page capture needs the Playwright package (pip install playwright)."
    if not (os.environ.get("ENCOREUM_CHROMIUM", os.environ.get("ORPHEUM_CHROMIUM")) or shutil.which("chromium")
            or shutil.which("chromium-browser") or shutil.which("google-chrome")):
        return "Page capture needs Chromium installed (apt install chromium)."
    return ""


def session_file(url: str) -> Path | None:
    """The saved login for this URL's host, if one exists. Sessions are per host
    and the browser only sends them to that origin, so one site's login is never
    offered to another."""
    host = (urlsplit(url).hostname or "").lower()
    path = config.SESSIONS_DIR / f"{host}.json"
    return path if host and path.is_file() else None


def page_capture_cmd(p: dict) -> list:
    """The command shared by the real job and the dry run, so they can't disagree."""
    cmd = ["python3", str(SCRIPTS / "capture-page-audio.py"), p["url"],
           "--prefix", short_title(p["show"]), "--title-max", str(CHAPTER_TITLE_MAX),
           "--archive", str(config.ARCHIVES_DIR / f"{p['show']}.txt")]
    if config.CHROMIUM_NO_SANDBOX:
        cmd.append("--no-sandbox")
    # Private addresses open up only when the PAGE ITSELF is on the trusted
    # list. Otherwise any public page could use an allowance like .plex.direct
    # to steer the browser at LAN services.
    host = (urlsplit(p["url"]).hostname or "").lower()
    if host in config.PAGE_CAPTURE_ALLOW_HOSTS:
        for allowed in config.PAGE_CAPTURE_ALLOW_HOSTS:
            cmd += ["--allow-host", allowed]
    session = session_file(p["url"])
    if session:
        cmd += ["--storage-state", str(session)]
    return cmd


def _download_page_capture(p: dict, show_dir: Path, control: JobControl, log) -> int:
    cmd = page_capture_cmd(p) + ["--out", str(show_dir), "--album", p["show"],
                                 # Beside the show folder, not in it: inside the
                                 # job's staging dir so it dies with staging, but
                                 # never published to the library.
                                 "--manifest", str(show_dir.parent / _CAPTURE_MANIFEST)]
    if p.get("author"):
        cmd += ["--artist", p["author"]]
    if p.get("include"):
        cmd += ["--items", ",".join(str(i) for i in p["include"])]
    log(f"$ capture-page-audio.py {_safe_url(p['url'])}")
    rc, _ = control.run(cmd, log)
    return rc


def _safe_url(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc.rsplit('@', 1)[-1]}{parts.path}"


def _archive_page_captures(p: dict, stage: Path, show_dir: Path) -> None:
    """Record finished captures only once they are really in the library. Staging
    is wiped on restart and the archive isn't, so recording earlier would recreate
    the 'archive says done, file is gone' failure documented in CLAUDE.md."""
    manifest = stage / _CAPTURE_MANIFEST
    if not manifest.is_file():
        return
    archive = config.ARCHIVES_DIR / f"{p['show']}.txt"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with open(archive, "a", encoding="utf-8") as f:
        for line in manifest.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            if (show_dir / entry["file"]).is_file():
                f.write(f"pagecapture {entry['key']}\n")


def _post_step(name, cmd, control, log) -> int:
    log("")
    log(f"--- {name} ---")
    log(f"$ {shlex.join(cmd)}")
    rc, _ = control.run(cmd, log)
    return rc


def _plain(cmd, log) -> int:
    """Run a child process that ignores cancellation (used after a live capture,
    where 'Cancel' means 'stop recording & keep the file', not 'discard')."""
    return subprocess.run(cmd, stdout=log.file, stderr=subprocess.STDOUT).returncode


def _live_state(p: dict, control: JobControl, log) -> tuple[str, int | None]:
    """
    Return (live_status, release_timestamp) for the URL.

    live_status is yt-dlp's FIVE-way field, not the is_live boolean this used to
    read. That distinction matters, and the boolean version was actively wrong:

        is_live      broadcasting right now      -> capture it
        post_live    just ended, still being     -> DO NOT download; YouTube has
                     processed by YouTube           not finished writing the file
        was_live     finished stream, now a VOD  -> normal download, real date
        not_live     an ordinary upload          -> normal download
        is_upcoming  scheduled, not started      -> wait for it

    `is_live` is False for BOTH post_live and is_upcoming, so the old code fell
    through to a normal download in exactly the two cases where that produces a
    broken or non-existent file. Verified against a real scheduled stream:
    live_status=is_upcoming while is_live=False.

    release_timestamp is the scheduled start (epoch seconds) when known.
    Returns ("", None) if the URL can't be read at all.
    """
    try:
        _, out = control.run(
            [config.YTDLP_BIN, "--no-warnings", "--skip-download",
             "--ignore-no-formats-error",          # upcoming streams have no formats yet
             "--print", "%(live_status)s|%(release_timestamp)s",
             "--playlist-items", "1", p["url"]], log, capture=True)
    except Cancelled:
        raise
    except Exception:  # noqa: BLE001
        return "", None
    line = next((ln.strip() for ln in reversed((out or "").splitlines())
                 if "|" in ln), "")
    if not line:
        return "", None
    status, _, ts = line.partition("|")
    status = status.strip()
    try:
        release = int(ts.strip())
    except (TypeError, ValueError):
        release = None
    return (status if status and status != "NA" else ""), release


def _wait_for_scheduled(p: dict, control: JobControl, log,
                        release_ts: int | None, max_wait_h: float = 26.0) -> bool:
    """
    Block until a scheduled stream actually goes live. Returns True once it is.

    yt-dlp's own --wait-for-video only helps on its download path; the live
    capture here is driven by streamlink, so the wait is done explicitly. Polls
    live_status rather than trusting the clock, because scheduled start times
    slip routinely. Cancellable at every step.
    """
    if release_ts:
        starts = datetime.fromtimestamp(release_ts)
        log(f"Scheduled: {starts:%Y-%m-%d %H:%M} "
            f"({(starts - datetime.now()).total_seconds() / 3600:.1f}h from now)")
    log("Waiting for the stream to start — this job stays parked until it does.")
    log("(Cancel any time; nothing is downloaded while waiting.)")

    # A fixed ceiling is wrong when the scheduled time is known: streams get
    # announced days ahead (the ACL test case was 3.7 days out), and a 26h cap
    # would have quietly given up two days before showtime. When we know when it
    # starts, wait until then plus a 2h grace for a late start; only fall back to
    # the flat ceiling when the schedule is unknown.
    if release_ts:
        deadline = max(release_ts + 7200, time.time() + 3600)
    else:
        deadline = time.time() + max_wait_h * 3600
    while time.time() < deadline:
        # Poll far apart when the start is distant, tighter when close — but
        # never tighter than ~1 min. streamlink runs with --hls-live-restart, so
        # a capture that begins a minute late STILL gets the stream from its
        # start. Catching the exact instant buys nothing and costs requests,
        # and YouTube rate-limits on request cadence (a tripped limit takes
        # about an hour to clear, which would lose the whole broadcast).
        if release_ts:
            remaining = release_ts - time.time()
            delay = 900 if remaining > 7200 else (300 if remaining > 900 else 60)
        else:
            delay = 300
        control.sleep(delay)
        status, release_ts = _live_state(p, control, log)
        if status == "is_live":
            log("Stream is LIVE — starting capture.")
            return True
        if status in ("was_live", "not_live"):
            log(f"Stream is no longer upcoming (now '{status}') — it will be "
                "downloaded as a normal video instead.")
            return False
        if status == "post_live":
            log("Stream has ended and YouTube is still processing it; "
                "waiting for the VOD rather than capturing.")
            return False
        log(f"…still {status or 'unknown'}; checking again shortly.")
    log("Gave up waiting — the stream never started "
        f"(waited past {datetime.fromtimestamp(deadline):%Y-%m-%d %H:%M}).")
    return False


def _capture_live(p: dict, show_dir: Path, control: JobControl, log) -> int:
    """
    Record a LIVE stream with streamlink into
        Season <this-year>/<Show> - S<year>E<mmdd> - <title>.mkv
    Cancelling stops the recording and keeps whatever was captured.
    """
    show, url = p["show"], p["url"]
    log(f"Show     : {show}")
    log("Mode     : live capture (streamlink)")
    log("=" * 46)

    log("Fetching stream info…")
    try:
        _, out = control.run([config.YTDLP_BIN, "--no-warnings", "-j",
                              "--ignore-no-formats-error",
                              "--playlist-items", "1", url], log, capture=True)
    except Cancelled:
        return 1
    info = {}
    for ln in (out or "").splitlines():
        if ln.lstrip().startswith("{"):
            try:
                info = json.loads(ln)
                break
            except ValueError:
                pass
    title = info.get("title") or show
    ls = info.get("live_status")
    if ls and ls != "is_live":
        log(f"NOTE: live_status is '{ls}', not 'is_live'; "
            "capturing whatever streamlink can pull.")

    today = date.today()
    y, mmdd = today.year, f"{today.month:02d}{today.day:02d}"
    season_dir = show_dir / f"Season {y}"
    season_dir.mkdir(parents=True, exist_ok=True)
    base = str(season_dir / f"{show} - S{y}E{mmdd} - {_clean_title(title)}")
    ts, mkv = Path(base + ".ts"), Path(base + ".mkv")

    # Sidecars so NFO/artwork work: minimal info.json (dated today) + thumbnail.
    try:
        Path(base + ".info.json").write_text(json.dumps({
            "title": title, "description": info.get("description", ""),
            "upload_date": today.strftime("%Y%m%d"), "id": info.get("id", ""),
            "webpage_url": info.get("webpage_url", url),
            "channel": info.get("channel") or info.get("uploader"),
            "uploader": info.get("uploader") or info.get("channel"),
            "duration": info.get("duration"),
        }), encoding="utf-8")
    except OSError:
        pass
    _plain([config.YTDLP_BIN, "--no-warnings", "--skip-download", "--write-thumbnail",
            "--convert-thumbnails", "jpg", "-o", base + ".%(ext)s", url], log)

    log("")
    log(f">>> Recording to {mkv.name}")
    log("    (runs until the stream ends, or click Cancel to stop and keep what's recorded)")
    cmd = ["streamlink", "--hls-live-restart", "--loglevel", "info", "-o", str(ts), url, "best"]
    log(f"$ {shlex.join(cmd)}")
    cancelled = False
    try:
        control.run(cmd, log)
    except Cancelled:
        cancelled = True
        log("")
        log("Recording stopped — finalising the file…")

    if not (ts.exists() and ts.stat().st_size > 0):
        log("ERROR: nothing was recorded (the stream may be offline or unsupported).")
        return 1

    log("Remuxing to MKV…")
    _plain(["ffmpeg", "-y", "-i", str(ts), "-c", "copy", str(mkv)], log)
    try:
        ts.unlink()
    except OSError:
        pass

    if p.get("run_nfo"):
        # --add-credits: also credit the source channel in NFOs that already
        # exist (plot line + tag only), keeping the show's channel list current.
        nfo = ["python3", str(SCRIPTS / "generate-nfo.py"), str(show_dir), "--show", show,
               "--add-credits"]
        for g in p.get("genres", []):
            nfo += ["--genre", g]
        if p.get("studio"):
            nfo += ["--studio", p["studio"]]
        log(""); log("--- Generate NFO metadata ---")
        _plain(nfo, log)
    if p.get("run_artwork", True):
        log(""); log("--- Generate Plex-native artwork ---")
        _plain(["python3", str(SCRIPTS / "generate-plex-art.py"), str(show_dir), "--show", show], log)

    # A live capture never goes through _publish() — it writes into the library
    # directly — so it has to report its own output file, or the capture record
    # would have nothing to attach a setlist to. This is THE festival path, so
    # losing it here would mean festival sets were the one case that never got
    # stage-2 metadata.
    p["_captured"] = [mkv] if mkv.exists() else []

    log("")
    log("Live capture finished." if not cancelled else "Live capture saved (stopped early).")
    return 0


def _publish(stage_show: Path, show_dir: Path, log) -> list[Path]:
    """Move completed files from staging into the library (same filesystem =
    instant rename). Skips partials and playlist-level junk.

    Returns the published paths rather than a count, because the caller needs to
    know WHICH file a capture produced in order to write its metadata sidecar
    next to it. A count cannot answer that.
    """
    if not stage_show.is_dir():
        return []
    moved: list[Path] = []
    for f in sorted(stage_show.rglob("*")):
        if not f.is_file():
            continue
        if f.suffix in (".part", ".ytdl") or f.name.endswith(".part") or "SNAENA" in f.name:
            continue
        rel = f.relative_to(stage_show)
        if rel.parts and rel.parts[0] == "Season NA":
            continue
        target = show_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(f, target)          # same-filesystem atomic rename
        except OSError:
            shutil.move(str(f), str(target))
        moved.append(target)
    log(f"Published {len(moved)} completed file(s) to {show_dir}.")
    return moved


def _post_process(p: dict, show_dir: Path, control: JobControl, log) -> None:
    """Rename/renumber + NFO + artwork, run in the library on the full show set."""
    if p.get("naming") in presets.AUDIO_NAMES:
        # No footage-date rename, no Kodi/Plex NFO, no Plex artwork -- those are
        # all TV-library concepts. Audiobookshelf/the Live Music library read
        # embedded tags + the folder itself; nothing here needs a Plex NFO agent.
        log("")
        log(f"{presets.label(p.get('naming'))}: skipping video rename/NFO/artwork post-processing.")
        return

    conflict = layout_conflict(p.get("naming", ""), bool(p.get("run_rename")), p.get("_layout_before"))
    if conflict:
        log("")
        log("--- Reorganise step SKIPPED ---")
        log(conflict)
    elif p.get("naming") == "performance":
        cmd = ["python3", str(SCRIPTS / "name-by-performance.py"),
               str(show_dir), "--show", p["show"], "--apply"]
        if p.get("tour"):
            cmd += ["--tour", p["tour"]]
        _post_step("Name by performance (date · tour · venue, in year folders)", cmd, control, log)
    elif p.get("naming") == "sequence":
        cmd = ["python3", str(SCRIPTS / "renumber-episodes.py"),
               str(show_dir), "--show", p["show"], "--apply"]
        _post_step("Renumber episodes (sequential by concert date)", cmd, control, log)
    elif p.get("run_rename"):
        cmd = ["python3", str(SCRIPTS / "rename-by-footage-date.py"),
               str(show_dir), "--show", p["show"], "--apply"]
        _post_step("Rename by footage date", cmd, control, log)

    if p.get("audio_only") and not (p.get("run_nfo") or p.get("run_artwork", True)):
        # Belt-and-suspenders: main._build_job_params() already forces both
        # off for audio_only, so this is just making the skip visible in the
        # log instead of silent (rename/renumber above still ran normally).
        log("")
        log("Audio only: skipping NFO/artwork (video-library metadata, not used for audio).")

    if p.get("run_nfo"):
        cmd = ["python3", str(SCRIPTS / "generate-nfo.py"),
               str(show_dir), "--show", p["show"], "--add-credits"]
        for g in p.get("genres", []):
            cmd += ["--genre", g]
        if p.get("studio"):
            cmd += ["--studio", p["studio"]]
        _post_step("Generate NFO metadata", cmd, control, log)

    if p.get("run_artwork", True):
        cmd = ["python3", str(SCRIPTS / "generate-plex-art.py"),
               str(show_dir), "--show", p["show"]]
        _post_step("Generate Plex-native artwork", cmd, control, log)


def _register_capture(p: dict, published: list, job_id: str, log) -> None:
    """Write stage-1 metadata for a recorded set, the moment it exists.

    Everything here is known BEFORE the recording starts — the app chose the
    artist, the stage and the stream — so this never waits on anything and never
    fails the job. A setlist is not part of it: at this instant nobody has
    posted one, and most sets never get one at all. captures.enrich_pass()
    attaches one later if it appears.

    Wrapped broadly on purpose. A metadata sidecar failing must not turn a
    successful recording into a failed job; the recording is the valuable part
    and it is already safely in the library by this point.
    """
    if not p.get("artist") and not p.get("segment_id"):
        return                      # an ordinary download, not a tracked set
    try:
        from . import captures, catalog
        video = None
        for f in published:
            if Path(f).suffix.lower() in (".mkv", ".mp4", ".webm", ".m4v"):
                video = Path(f)
                break
        if video is None:
            return

        seg = catalog.get_segment(p["segment_id"]) if p.get("segment_id") else None
        if seg:
            # The performance date must be the festival-LOCAL date, not the
            # capture's UTC date: a set running past midnight local is already
            # tomorrow in UTC, and setlist.fm is keyed on the local day.
            perf = captures.local_date(seg["starts_at"],
                                       seg.get("timezone") or "UTC")
            cid = captures.record(
                seg["artist"], segment_id=seg["id"], job_id=job_id,
                event_id=seg.get("event_id") or "",
                artist_mbid=seg.get("artist_mbid") or "",
                festival=seg.get("festival") or "", stage=seg.get("stage") or "",
                show=seg.get("show") or p.get("show", ""),
                performance_date=perf, starts_at=seg["starts_at"],
                ends_at=seg.get("ends_at") or 0,
                stream_url=seg.get("stream_url") or p.get("url", ""),
                file_path=str(video))
        else:
            cid = captures.record(
                p.get("artist") or p.get("show", ""), job_id=job_id,
                show=p.get("show", ""), performance_date=p.get("performance_date", ""),
                venue=p.get("venue", ""), stream_url=p.get("url", ""),
                file_path=str(video))
        captures.write_nfo(captures.get(cid))
        log(f"Recorded metadata for this set (capture {cid}).")
        log("A setlist will be attached automatically if one is posted later.")
    except Exception as e:  # noqa: BLE001
        log(f"Note: could not write set metadata ({e}). The recording is fine.")


def run_job(p: dict, show_dir: Path, control: JobControl, log, job_id: str = "job") -> int:
    """
    Run the full pipeline for one job. Returns an overall returncode
    (0 = success). Raises Cancelled if the user cancels.
    """
    # A sibling of the staging dir (not inside it): the staged path below
    # rmtree()s its own stage dir BEFORE _post_process() runs its scripts, and
    # those still need a cwd that exists. Same filesystem, so init_db()'s
    # startup wipe of STAGING_DIR clears any left behind by a crash.
    scratch = config.STAGING_DIR / f"{job_id}.cwd"
    try:
        scratch.mkdir(parents=True, exist_ok=True)
        control.cwd = str(scratch)
    except OSError as e:
        log(f"WARNING: couldn't create scratch dir {scratch}: {e}")
    try:
        return _run_job(p, show_dir, control, log, job_id)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _run_job(p: dict, show_dir: Path, control: JobControl, log, job_id: str) -> int:
    show_dir.mkdir(parents=True, exist_ok=True)
    published: list[Path] = []
    # Snapshot BEFORE anything downloads: this job's own output will land in
    # the same folder, so the max has to be read while the folder still only
    # reflects what existed before this run (continue_sequence() uses it to
    # tell "new batch, renumber past this" from "same growing playlist,
    # already past this" after publish).
    existing_max = existing_max_seq(show_dir) if p.get("naming") in presets.AUDIO_NAMES else 0
    # Also BEFORE anything downloads: this job's own files must not count.
    p["_layout_before"] = existing_layout(show_dir)
    if p.get("live"):
        status, release_ts = _live_state(p, control, log)
        log(f"Live check: live_status={status or 'unknown'}")

        if status == "is_live":
            rc = _capture_live(p, show_dir, control, log)
            _register_capture(p, p.get("_captured", []), job_id, log)
            return rc

        if status == "is_upcoming":
            # Scheduled but not started. The old boolean check reported False here
            # and fell through to a normal download of a video that does not exist
            # yet — which fails with "this live event will begin in N days".
            if _wait_for_scheduled(p, control, log, release_ts):
                rc = _capture_live(p, show_dir, control, log)
                _register_capture(p, p.get("_captured", []), job_id, log)
                return rc
            log("Not capturing live; continuing as a normal download.")

        elif status == "post_live":
            # The stream ended but YouTube is still assembling the VOD. Downloading
            # now yields a file that is still being written. Wait it out.
            log("Stream has just ended and YouTube is still processing the recording.")
            log("Waiting for the finished VOD rather than grabbing a partial file…")
            for _ in range(30):                     # ~30 min, cancellable
                control.sleep(60)
                status, _ = _live_state(p, control, log)
                if status in ("was_live", "not_live"):
                    log("VOD is ready — downloading it now.")
                    break
                log(f"…still {status or 'unknown'}.")
            else:
                log("Still processing after 30 minutes; downloading anyway — "
                    "re-run later if the file looks short.")

        elif status in ("was_live", "not_live"):
            log("This URL is a past stream / ordinary video, not a live broadcast — "
                "downloading it normally with its real date.")
        else:
            log("Could not determine live status; treating it as a normal download.")
        # fall through to the normal VOD download below

    direct = bool(p.get("direct"))
    log(f"Show     : {p['show']}")
    log(f"Mode     : {p.get('naming', 'datecode')}")
    log(f"Save to  : {show_dir}")

    if direct:
        # Download straight into the library, then sweep any failed-item partials.
        log("Workspace: library (direct)")
        log("=" * 46)
        rc = _download(p, show_dir, control, log)
        try:
            swept = library.sweep_partials(str(show_dir.relative_to(config.LIBRARY_ROOT)))
            if swept["removed"]:
                log(f"Cleaned {swept['removed']} leftover partial file(s) "
                    f"({swept['bytes'] // 1_000_000} MB freed).")
        except (ValueError, OSError):
            pass
    else:
        # Download into a same-filesystem staging area; only completed files are
        # published to the library. Partials/failed items die in staging.
        stage = config.STAGING_DIR / job_id
        stage_show = stage / p["show"]
        stage_show.mkdir(parents=True, exist_ok=True)
        log("Workspace: staging → publishes to library on completion")
        log("=" * 46)
        try:
            rc = _download(p, stage_show, control, log)
        finally:
            # Publish whatever completed — even on cancel — so nothing recorded in
            # the download-archive is stranded, then clear staging.
            try:
                published = _publish(stage_show, show_dir, log)
                if p.get("page_capture"):
                    # Before continue_sequence(), which renames what was just
                    # published: the manifest holds the names as captured.
                    _archive_page_captures(p, stage, show_dir)
                if existing_max:
                    # Direct mode (above) skips this: it writes straight into
                    # show_dir with no staging/publish step, so there's no
                    # `published` list to tell "new this job" from "was
                    # already there" -- renumbering needs that distinction.
                    continue_sequence(published, existing_max, log)
            finally:
                shutil.rmtree(stage, ignore_errors=True)

    _post_process(p, show_dir, control, log)
    _register_capture(p, published, job_id, log)
    log("")
    log("Pipeline finished.")
    return rc
