"""
updater.py -- keep yt-dlp current (requirement #3).

yt-dlp lives in a user-owned venv on the persistent /config volume, so the
non-root app can upgrade it with pip at any time -- on a daily schedule and via
the "Update now" button. ffmpeg and OS libraries update by rebuilding the image
(documented in the README); we never apt-upgrade a running container.
"""
import subprocess

from . import config


def _run(cmd) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def ytdlp_version() -> str:
    try:
        r = _run([config.YTDLP_BIN, "--version"])
        return r.stdout.strip() or "unknown"
    except FileNotFoundError:
        return "not installed"


def ffmpeg_version() -> str:
    try:
        r = _run(["ffmpeg", "-version"])
        first = r.stdout.splitlines()[0] if r.stdout else ""
        return first.replace("ffmpeg version", "").strip() or "unknown"
    except FileNotFoundError:
        return "not installed"


def update_ytdlp() -> dict:
    """pip -U yt-dlp inside the venv; log the output and return fresh versions."""
    r = _run([config.VENV_PYTHON, "-m", "pip", "install", "--upgrade", "yt-dlp"])
    try:
        (config.CONFIG_DIR / "last-update.log").write_text(
            (r.stdout or "") + (r.stderr or ""), encoding="utf-8"
        )
    except OSError:
        pass
    return {
        "ok": r.returncode == 0,
        "yt_dlp": ytdlp_version(),
        "ffmpeg": ffmpeg_version(),
    }
