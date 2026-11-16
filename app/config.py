"""
config.py -- environment, paths, defaults, and the library write-guardrail.

Everything the rest of the app needs to know about *where* things live and
*which* directories it is allowed to write into lives here.
"""
import os
from pathlib import Path

# --- directories inside the container (backed by the /config and /library volumes)
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
LOGS_DIR = CONFIG_DIR / "logs"
ARCHIVES_DIR = CONFIG_DIR / "archives"
DB_PATH = CONFIG_DIR / "jobs.db"
# Saved browser logins for page capture, one file per host (<host>.json). These
# hold live login tokens, so treat this folder like a secret.
SESSIONS_DIR = CONFIG_DIR / "sessions"

# Page capture refuses private addresses, because a web page is untrusted input.
# Hosts listed here are exempt, and only for a job whose page is on this list.
# '.example.com' also matches subdomains.
PAGE_CAPTURE_ALLOW_HOSTS = [
    h.strip().lower()
    for h in os.environ.get("PAGE_CAPTURE_ALLOW_HOSTS", "").split(",")
    if h.strip()
]
# Docker's default seccomp profile can stop Chromium's sandbox from starting.
CHROMIUM_NO_SANDBOX = os.environ.get("CHROMIUM_NO_SANDBOX", "").lower() in ("1", "true", "yes")

# yt-dlp is installed in a user-owned venv on the persistent /config volume so
# the non-root app can self-update it (see entrypoint.sh + updater.py).
VENV_DIR = Path(os.environ.get("VENV_DIR", str(CONFIG_DIR / "venv")))
YTDLP_BIN = os.environ.get("YTDLP_BIN", str(VENV_DIR / "bin" / "yt-dlp"))
VENV_PYTHON = os.environ.get("VENV_PYTHON", str(VENV_DIR / "bin" / "python"))

# --- the write-whitelist (requirement #5 guardrail)
ALLOWED_ROOTS = [
    Path(p).resolve()
    for p in os.environ.get("ALLOWED_ROOTS", "/library").split(",")
    if p.strip()
]
# The mounted media library root (e.g. $MEDIADIR1 = /mnt/nas/media). Shows are
# created under a *destination* subfolder of this (concerts, tv, tv-podcasts, ...).
LIBRARY_ROOT = ALLOWED_ROOTS[0] if ALLOWED_ROOTS else Path("/library")
# Back-compat alias.
DEFAULT_LIBRARY = LIBRARY_ROOT

# Default destination subfolder; overridable per-job and savable as the default.
DEFAULT_DEST = os.environ.get("DEFAULT_DEST", "concerts").strip().strip("/")

# Jobs download into here first (same filesystem as the library, so publishing
# is an instant rename), then only completed files are moved into the library.
# Hidden + a sibling of the Plex library subfolders, so Plex never scans it.
STAGING_DIR = LIBRARY_ROOT / ".encoreum-staging"

# --- job defaults (handoff section 7)
DEFAULTS = {
    "naming": "performance",   # 'YYYY.MM.DD - tour - venue - NNN' in bare year folders
    "quality": "max",
    "reverse": True,
    "run_rename": True,
    "run_nfo": True,
    "genres": ["Music", "Concert"],
}


def ensure_dirs() -> None:
    """Create the config sub-directories (best-effort; entrypoint already does this)."""
    for d in (CONFIG_DIR, LOGS_DIR, ARCHIVES_DIR):
        d.mkdir(parents=True, exist_ok=True)


def safe_join(base: Path, *parts: str) -> Path:
    """
    Resolve ``base/parts`` and guarantee the result stays inside one of
    ALLOWED_ROOTS. Raises ValueError on any attempt to escape (e.g. ``../``).

    This is the single choke-point every write path passes through.
    """
    candidate = base.joinpath(*[p for p in parts if p]).resolve()
    for root in ALLOWED_ROOTS:
        try:
            candidate.relative_to(root)
            return candidate
        except ValueError:
            continue
    raise ValueError(
        f"Refusing to write to '{candidate}': outside ALLOWED_ROOTS "
        f"({', '.join(str(r) for r in ALLOWED_ROOTS)})."
    )


def clean_dest(destination: str) -> str:
    """Normalise a destination subfolder (may be nested, e.g. 'tv/live')."""
    return (destination or "").strip().strip("/")


def resolve_dest_dir(destination: str) -> Path:
    """The (validated) destination folder: <LIBRARY_ROOT>/<destination>."""
    dest = clean_dest(destination) or DEFAULT_DEST
    return safe_join(LIBRARY_ROOT, dest) if dest else LIBRARY_ROOT


def resolve_show_dir(show: str, destination: str = "") -> Path:
    """
    Work out (and validate) the show folder for a job:
        <LIBRARY_ROOT>/<destination>/<show>

    ``show`` and ``destination`` are user input, so they go through safe_join,
    which rejects anything that resolves outside ALLOWED_ROOTS.
    """
    show = (show or "").strip().strip("/")
    if not show:
        raise ValueError("Show name is required.")
    return safe_join(resolve_dest_dir(destination), show)
