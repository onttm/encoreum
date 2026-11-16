"""
history.py -- a small persisted list of recently used URLs.

Captured on both preview and download so you can re-pick a URL quickly (handy
when troubleshooting the same playlist repeatedly). Deduped by URL, most-recent
first, capped. Stored at /config/url_history.json.
"""
import json
import threading

from . import config

_PATH = config.CONFIG_DIR / "url_history.json"
_lock = threading.Lock()
MAX = 20


def list_urls(limit: int = MAX) -> list:
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data[:limit]
    except (OSError, ValueError):
        pass
    return []


def add(url: str, show: str = "") -> None:
    url = (url or "").strip()
    if not url:
        return
    with _lock:
        items = [it for it in list_urls(MAX) if it.get("url") != url]
        items.insert(0, {"url": url, "show": (show or "").strip()})
        try:
            config.ensure_dirs()
            _PATH.write_text(json.dumps(items[:MAX], indent=2), encoding="utf-8")
        except OSError:
            pass
