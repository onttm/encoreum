"""
settings.py -- tiny persisted key/value store for user preferences.

Currently just the default *destination* subfolder (concerts / tv / tv-podcasts /
whatever the user picks). Saved to /config/settings.json so it survives restarts.
"""
import json
import threading

from . import config

_PATH = config.CONFIG_DIR / "settings.json"
_lock = threading.Lock()
_DEFAULTS = {"destination": config.DEFAULT_DEST, "addons": []}


def load() -> dict:
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        return {**_DEFAULTS, **data}
    except (OSError, ValueError):
        return dict(_DEFAULTS)


def save(**changes) -> dict:
    with _lock:
        cur = load()
        cur.update({k: v for k, v in changes.items() if v is not None})
        config.ensure_dirs()
        _PATH.write_text(json.dumps(cur, indent=2), encoding="utf-8")
        return cur


def addons_enabled() -> set:
    """Add-on presets switched on (see presets.ADDON_NAMES). Empty by default."""
    return {a for a in load().get("addons") or [] if isinstance(a, str)}


def set_addon(name: str, on: bool) -> set:
    cur = addons_enabled()
    cur = (cur | {name}) if on else (cur - {name})
    save(addons=sorted(cur))
    return cur


def destination() -> str:
    return config.clean_dest(load().get("destination")) or config.DEFAULT_DEST
