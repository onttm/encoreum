"""
secrets.py -- read credentials from files, not from the environment.

WHY NOT ENVIRONMENT VARIABLES
    An env var holding a credential is readable in more places than people
    expect, and all of them apply to this app:

      * `docker inspect <container>` prints it in full, so anyone in the
        `docker` group on the host has it — and membership of that group is
        already root-equivalent, but this hands them the key to OTHER services
        too.
      * It is inherited by every child process. This app spawns yt-dlp,
        streamlink and ffmpeg, so a Plex token set as an env var is in the
        environment of every one of them.
      * It lands in `/proc/<pid>/environ`, in crash dumps that capture the
        environment, and in any log that dumps config.

    A file mounted at /run/secrets/ has none of those properties: it is not in
    the process environment, not in `docker inspect`, and not inherited.

    This was applied inconsistently at first — setlist.fm was moved to a secret
    while the Plex token and the Lidarr key stayed as plain env vars, which is
    the worst of both: the discipline was documented but not followed, and the
    Plex token is precisely the credential that leaked earlier.

THE _FILE CONVENTION
    `read("PLEX_TOKEN")` looks, in order, at:

      1. $PLEX_TOKEN_FILE          -> read that path        (preferred)
      2. /run/secrets/plex_token   -> conventional location
      3. $PLEX_TOKEN               -> plain env var         (discouraged)

    Step 3 stays so the app still runs for someone who has not set up secrets —
    a self-hosted tool that refuses to start without Docker secrets is worse for
    the person inheriting it — but `source_of()` reports which was used so the
    UI can say plainly that a credential is sitting in an env var.

    The same convention as postgres, mysql and nextcloud images, so it is one
    fewer thing to learn.

    NOTE: Compose ignores `uid`/`gid`/`mode` on a secret definition. Verified on
    this host — it prints "secrets `uid`, `gid` and `mode` are not supported,
    they will be ignored" and mounts the file with the HOST file's ownership.
    So permissions are managed on the host file and the container is joined to
    its group in entrypoint.sh.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

SECRETS_DIR = Path(os.environ.get("SECRETS_DIR", "/run/secrets"))

# Values change rarely and are read often; re-reading a file per call is
# pointless I/O. Not cached forever, so a rotated secret takes effect without a
# restart.
_TTL = 60.0
_cache: dict[str, tuple[float, str]] = {}


def _conventional_path(name: str) -> Path:
    return SECRETS_DIR / name.lower()


def read(name: str) -> str:
    """The credential for `name`, from a file if possible, else the env var."""
    now = time.time()
    hit = _cache.get(name)
    if hit and now - hit[0] < _TTL:
        return hit[1]

    value = ""
    path = os.environ.get(f"{name}_FILE", "").strip()
    for p in ([Path(path)] if path else []) + [_conventional_path(name)]:
        try:
            # .strip() matters: an editor that appends a newline would otherwise
            # send a credential with a trailing \n, which most APIs reject as a
            # flat 401/403 with nothing pointing at whitespace as the cause.
            value = p.read_text(encoding="utf-8").strip()
            if value:
                break
        except (OSError, UnicodeDecodeError):
            continue
    if not value:
        value = os.environ.get(name, "").strip()

    _cache[name] = (now, value)
    return value


def source_of(name: str) -> str:
    """Where `name` came from — for diagnostics that must never print it.

    "Configured but unreadable" and "not configured" are indistinguishable to a
    caller otherwise, and unreadable is the likely failure: Compose mounts a
    secret with the host file's ownership, and this container runs as PUID.
    """
    path = os.environ.get(f"{name}_FILE", "").strip()
    candidates = ([Path(path)] if path else []) + [_conventional_path(name)]
    for p in candidates:
        try:
            if p.read_text(encoding="utf-8").strip():
                return f"docker secret ({p})"
            return f"secret file {p} is present but EMPTY"
        except PermissionError:
            return (f"secret file {p} exists but is NOT READABLE by this "
                    "container — Compose mounts it with the host file's "
                    "ownership, so the app user must be in that file's group "
                    "(entrypoint.sh does this)")
        except OSError:
            continue
    if os.environ.get(name, "").strip():
        return (f"environment variable {name} — visible in `docker inspect` and "
                "inherited by child processes; prefer a docker secret")
    return "not configured"


def audit() -> list[dict]:
    """Every credential and where it came from. Never includes a value."""
    return [{"name": n, "configured": bool(read(n)), "source": source_of(n),
             "secure": source_of(n).startswith("docker secret")}
            for n in ("PLEX_TOKEN", "LIDARR_API_KEY", "SETLISTFM_API_KEY")]
