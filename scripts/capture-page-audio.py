#!/usr/bin/env python3
"""
capture-page-audio.py -- save the audio a web page's own player streams.

For pages yt-dlp has no extractor for. Opens the page in a real (headless)
Chromium, finds what its audio player loads, and saves it. Standalone: Encoreum
shells out to it, and it works the same from a terminal.

    ./capture-page-audio.py URL --list                 # show what would be saved
    ./capture-page-audio.py URL --out ~/audio          # save it

HOW AUDIO IS FOUND, in order of preference
    direct     <audio src> or a network response that is an audio file. Fetched
               again from the start with the page's cookies, because a player's
               own requests are partial 206 ranges, not the whole file.
    manifest   Unencrypted HLS/DASH. ffmpeg stream-copies the audio track.
    blob       <audio src="blob:..."> holding a complete file in page memory.
    mse        Media Source Extensions chunks, copied as the player appends
               them. Only works by playing the whole thing, in real time.

WHAT IT REFUSES, on purpose
    * DRM. A page requesting Encrypted Media Extensions, or a manifest declaring
      encryption, is reported and skipped. There is no way around this flag.
    * Anti-bot pages. A block or challenge is reported as what it is. There is
      no stealth mode, no spoofed browser identity, no CAPTCHA handling.
    * Private addresses, unless named with --allow-host. A page is untrusted
      input, and without this it could steer the browser at LAN services.

Secrets: URLs are logged without their query strings, because that is where
stream tokens live (Plex's X-Plex-Token, for one). Pass logins with
--storage-state, never in the URL.

Check that you may save what you point this at. Many streaming sites' terms
forbid it, and this script cannot tell.

Requires: ffmpeg/ffprobe, `pip install playwright`, and a Chromium (`apt install
chromium`, or `playwright install chromium`). No Claude/Anthropic dependency.

Exit codes: 0 ok; 1 some items failed; 2 nothing capturable; 3 not installed.
"""
import argparse
import base64
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AUDIO_EXTS = {".mp3", ".m4a", ".m4b", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wav"}
MANIFEST_EXTS = {".m3u8", ".mpd"}
CTYPE_EXT = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a",
             "audio/x-m4a": ".m4a", "audio/aac": ".aac", "audio/ogg": ".ogg",
             "audio/opus": ".opus", "audio/flac": ".flac", "audio/x-flac": ".flac",
             "audio/wav": ".wav", "audio/x-wav": ".wav"}
MANIFEST_CTYPES = ("mpegurl", "dash+xml")
# Codec -> (extension, ffmpeg muxer) for audio that arrives without a usable
# container of its own (MSE chunks, HLS/DASH segments). Never re-encoded.
CODEC_OUT = {"aac": (".m4a", "ipod"), "alac": (".m4a", "ipod"), "mp3": (".mp3", "mp3"),
             "opus": (".opus", "opus"), "vorbis": (".ogg", "ogg"), "flac": (".flac", "flac")}
# Muxer per extension, for tagging an already-downloaded file in place. Named
# explicitly because work-in-progress files end in .part, which ffmpeg cannot
# infer a format from -- and .part is what keeps them out of the library.
EXT_MUXER = {".mp3": "mp3", ".m4a": "ipod", ".m4b": "ipod", ".aac": "adts",
             ".ogg": "ogg", ".oga": "ogg", ".opus": "opus", ".flac": "flac", ".wav": "wav"}
ARCHIVE_PREFIX = "pagecapture"

# Installed before any page script runs. Reports only: it copies what the player
# hands the browser and never alters playback or media keys.
HOOKS = r"""
(() => {
  const toB64 = (v) => {
    let bin = '';
    for (let i = 0; i < v.length; i += 0x8000)
      bin += String.fromCharCode.apply(null, v.subarray(i, i + 0x8000));
    return btoa(bin);
  };
  if (navigator.requestMediaKeySystemAccess) {
    const orig = navigator.requestMediaKeySystemAccess.bind(navigator);
    navigator.requestMediaKeySystemAccess = (...a) => {
      window.__encoreumDrm(String(a[0]));
      return orig(...a);
    };
  }
  const els = (window.__encoreumEls = new Set());
  const play = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () { els.add(this); return play.call(this); };
  if (!window.MediaSource) return;
  const ids = new WeakMap(); let next = 0;
  const add = MediaSource.prototype.addSourceBuffer;
  MediaSource.prototype.addSourceBuffer = function (mime) {
    const sb = add.call(this, mime);
    ids.set(sb, { id: next++, mime: String(mime) });
    return sb;
  };
  const append = SourceBuffer.prototype.appendBuffer;
  SourceBuffer.prototype.appendBuffer = function (data) {
    const m = ids.get(this);
    if (m && m.mime.startsWith('audio/')) {
      const v = data instanceof ArrayBuffer ? new Uint8Array(data)
        : new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
      window.__encoreumChunk(m.id, m.mime, toB64(v));
    }
    return append.call(this, data);
  };
  const eos = MediaSource.prototype.endOfStream;
  MediaSource.prototype.endOfStream = function (...a) {
    if (!a[0]) window.__encoreumEos();
    return eos.apply(this, a);
  };
})();
"""

SCAN = r"""
() => [...document.querySelectorAll('audio')].flatMap(a => {
  const urls = [a.currentSrc, a.src,
                ...[...a.querySelectorAll('source')].map(s => s.src)].filter(Boolean);
  return [...new Set(urls)].slice(0, 1).map(src => ({
    src, title: a.title || a.getAttribute('aria-label') || a.dataset.title || ''
  }));
})
"""

KICK = r"""
() => {
  for (const a of document.querySelectorAll('audio')) {
    a.muted = true;
    const p = a.play();
    if (p && p.catch) p.catch(() => {});
  }
}
"""

PLAY_SELECTORS = ('[data-testid="preplay-play"]', 'button[aria-label="Play" i]',
                  'button[aria-label^="Play" i]', 'button[title^="Play" i]',
                  '[role="button"][aria-label^="Play" i]', 'button[class*="play" i]',
                  '[class*="play-button" i]', '[class*="playButton" i]')

FINISHED = r"""
() => {
  const all = [...document.querySelectorAll('audio'), ...(window.__encoreumEls || [])];
  return all.length > 0 && all.every(a => a.ended);
}
"""

BLOB_READ = r"""
async (src) => {
  const b = await (await fetch(src)).blob();
  const buf = new Uint8Array(await b.arrayBuffer());
  let bin = '';
  for (let i = 0; i < buf.length; i += 0x8000)
    bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
  return { type: b.type, data: btoa(bin) };
}
"""


class Log:
    """Progress goes to stdout, unless stdout is carrying the --json result."""

    def __init__(self, to_stderr: bool):
        self.stream = sys.stderr if to_stderr else sys.stdout

    def __call__(self, msg: str = "") -> None:
        print(msg, file=self.stream, flush=True)


def safe_url(url: str) -> str:
    """A URL fit for a log: never the query string or fragment."""
    if url.startswith(("blob:", "data:")):
        return url.split(":", 1)[0] + ":…"
    p = urllib.parse.urlsplit(url)
    host = p.hostname or ""
    return f"{p.scheme}://{host}{':%d' % p.port if p.port else ''}{p.path}" + ("?…" if p.query else "")


def clean_name(text: str, limit: int = 80) -> str:
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", text or "")
    return re.sub(r"\s+", " ", text).strip(" .")[:limit].rstrip(" .")


def url_key(url: str) -> str:
    """Identity of a source: host + path. Query strings are excluded because
    signed/expiring stream URLs change on every load while the file does not."""
    p = urllib.parse.urlsplit(url)
    return hashlib.sha1(f"{p.hostname}{p.path}".encode()).hexdigest()[:16]


class Guard:
    """Refuses private/loopback/link-local destinations unless allowed by name."""

    def __init__(self, allow_hosts=()):
        self.allow = [h.lower() for h in allow_hosts]
        self.cache: dict[str, bool] = {}

    def _named(self, host: str) -> bool:
        return any(host == a or (a.startswith(".") and host.endswith(a)) for a in self.allow)

    def blocked(self, url: str) -> bool:
        p = urllib.parse.urlsplit(url)
        if p.scheme not in ("http", "https"):
            return True
        host = (p.hostname or "").lower()
        if not host or self._named(host):
            return not host
        if host not in self.cache:
            try:
                addrs = {i[4][0] for i in socket.getaddrinfo(host, None)}
            except OSError:
                addrs = set()       # unresolvable: the request simply fails
            self.cache[host] = any(not ipaddress.ip_address(a.split("%")[0]).is_global
                                   for a in addrs)
        return self.cache[host]


class GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """Applies the guard to every hop, and never carries a site's cookies to a
    different host."""

    def __init__(self, guard: Guard):
        self.guard = guard

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if self.guard.blocked(newurl):
            raise urllib.error.URLError(f"redirect to a blocked address: {safe_url(newurl)}")
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new and urllib.parse.urlsplit(newurl).hostname != urllib.parse.urlsplit(req.full_url).hostname:
            new.remove_header("Cookie")
        return new


def fetch(url, headers, guard, timeout=60):
    if guard.blocked(url):
        raise urllib.error.URLError(f"blocked address: {safe_url(url)}")
    opener = urllib.request.build_opener(GuardedRedirect(guard))
    return opener.open(urllib.request.Request(url, headers=headers), timeout=timeout)


# --- media helpers ----------------------------------------------------------
def probe(path: Path) -> dict | None:
    """The file's audio stream info, or None if it isn't playable audio. This is
    what stops a saved HTML error page being published as a chapter."""
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
                        "-show_format", str(path)], capture_output=True, text=True)
    try:
        info = json.loads(r.stdout or "{}")
    except ValueError:
        return None
    streams = info.get("streams") or []
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not audio:
        return None
    return {"codec": audio.get("codec_name", ""),
            "duration": float((info.get("format") or {}).get("duration") or 0),
            "has_video": any(s.get("codec_type") == "video"
                             and not (s.get("disposition") or {}).get("attached_pic")
                             for s in streams)}


def stream_copy(src: Path, dst: Path, muxer: str, meta: dict) -> bool:
    """Audio-only stream copy with optional tags. Never re-encodes."""
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-map", "0:a:0", "-c", "copy"]
    for k, v in meta.items():
        if v:
            cmd += ["-metadata", f"{k}={v}"]
    cmd += ["-f", muxer, str(dst)]
    return subprocess.run(cmd).returncode == 0 and dst.is_file() and dst.stat().st_size > 0


def manifest_problem(url, headers, guard, depth=0) -> str | None:
    """Why this HLS/DASH manifest can't be used: encrypted, or pointing at a
    blocked address. ffmpeg does its own fetching, past the browser's guard, so
    every URL it would follow has to be checked here first."""
    with fetch(url, headers, guard) as r:
        text = r.read(4_000_000).decode("utf-8", "replace")
    if re.search(r"<ContentProtection\b", text, re.I):
        return "the DASH manifest declares content protection (DRM)"
    if re.search(r"#EXT-X-(SESSION-)?KEY:(?!.*METHOD=NONE)", text):
        return "the HLS stream is encrypted"
    refs = [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.startswith("#") and not ln.lstrip().startswith("<")]
    refs += re.findall(r'URI="([^"]+)"', text)
    refs += re.findall(r"<BaseURL>([^<]+)</BaseURL>", text)
    absolute = [urllib.parse.urljoin(url, u) for u in refs]
    for u in absolute:
        if guard.blocked(u):
            return f"it references a blocked address ({safe_url(u)})"
    if depth < 2:
        for u in [u for u in absolute if urllib.parse.urlsplit(u).path.endswith(".m3u8")][:8]:
            problem = manifest_problem(u, headers, guard, depth + 1)
            if problem:
                return problem
    return None


# --- browser ----------------------------------------------------------------
def find_chromium() -> str | None:
    return os.environ.get("ENCOREUM_CHROMIUM", os.environ.get("ORPHEUM_CHROMIUM")) or next(
        (p for p in map(shutil.which, ("chromium", "chromium-browser", "google-chrome")) if p), None)


def inspect_page(args, guard, log, spool: Path | None) -> dict:
    """Load the page and report what its player uses. With spool set, MSE chunks
    are kept and playback runs until the stream ends."""
    from playwright.sync_api import Error as PWError, TimeoutError as PWTimeout, sync_playwright

    found = {"network": {}, "manifests": {}, "drm": [], "mse": {}, "eos": False,
             "dom": [], "status": 0, "title": "", "cookies": {}, "ua": "", "finished": False,
             "blobs": {}, "blocked": set()}

    def on_chunk(sid, mime, b64):
        rec = found["mse"].setdefault(int(sid), {"mime": mime, "bytes": 0, "t": 0.0})
        data = base64.b64decode(b64)
        rec["bytes"] += len(data)
        rec["t"] = time.time()
        if spool:
            with open(spool / f".mse-{sid}.part", "ab") as f:
                f.write(data)

    def on_response(resp):
        path = urllib.parse.urlsplit(resp.url).path
        ext = Path(path).suffix.lower()
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ext in MANIFEST_EXTS or any(c in ctype for c in MANIFEST_CTYPES):
            found["manifests"].setdefault(resp.url, ctype)
        elif ctype.startswith("audio/") or ext in AUDIO_EXTS:
            if resp.status < 400:
                found["network"].setdefault(resp.url, ctype)

    def guard_route(route):
        if guard.blocked(route.request.url):
            found["blocked"].add(safe_url(route.request.url))
            route.abort("blockedbyclient")
        else:
            route.continue_()

    with sync_playwright() as pw:
        browser = None
        if args.cdp:
            # Attach to a browser someone already has open (and logged in),
            # started with --remote-debugging-port. Only ever opens and closes
            # its own tab there.
            browser = pw.chromium.connect_over_cdp(args.cdp)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        else:
            launch_args = ["--autoplay-policy=no-user-gesture-required", "--mute-audio",
                           "--disable-dev-shm-usage"]
            # Explicit, because Playwright's default is OFF: left alone it passes
            # --no-sandbox itself, whatever this script's own flag says.
            browser = pw.chromium.launch(executable_path=find_chromium(), headless=True,
                                         chromium_sandbox=not args.no_sandbox, args=launch_args)
            ctx = browser.new_context(
                service_workers="block",      # they would bypass the guard below
                storage_state=args.storage_state or None)
        ctx.expose_function("__encoreumChunk", on_chunk)
        ctx.expose_function("__encoreumDrm", lambda ks: found["drm"].append(ks))
        ctx.expose_function("__encoreumEos", lambda: found.update(eos=True))
        ctx.add_init_script(HOOKS)
        ctx.route("**/*", guard_route)

        page = ctx.new_page()
        page.on("response", on_response)
        try:
            resp = page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)
            found["status"] = resp.status if resp else 0
            if found["status"] < 400:
                _find_sources(page, found, args, log)
                if spool and found["mse"] and not _has_file_source(found):
                    _play_through(page, found, args, log)
                for item in found["dom"]:
                    if item["src"].startswith("blob:") and spool and not found["mse"]:
                        try:
                            found["blobs"][item["src"]] = page.evaluate(BLOB_READ, item["src"])
                        except PWError:
                            pass
            found["title"] = page.title() or ""
            for u in list(found["network"]) + list(found["manifests"]) \
                    + [i["src"] for i in found["dom"] if i["src"].startswith("http")]:
                found["cookies"][u] = "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies(u))
            found["ua"] = page.evaluate("navigator.userAgent")
        except PWTimeout:
            found["error"] = "the page did not load within 60 seconds"
        except PWError as e:
            found["error"] = str(e).splitlines()[0][:200]
        finally:
            page.close()
            if not args.cdp:
                browser.close()
    return found


def _has_file_source(found) -> bool:
    return bool(found["network"] or found["manifests"]
                or any(i["src"].startswith("http") for i in found["dom"]))


def _find_sources(page, found, args, log) -> None:
    """Look before touching anything: a page that already names its audio needs
    no playback at all. Only then start the player -- a scripted play(), and
    then the page's own Play button, which is what most app-style players
    (including Plex Web) need before they request any media."""
    from playwright.sync_api import Error as PWError

    def scan():
        out = []
        for frame in page.frames:
            try:
                out += frame.evaluate(SCAN)
            except PWError:
                pass
        seen, unique = set(), []
        for i in out:
            if i["src"] not in seen:
                seen.add(i["src"])
                unique.append(i)
        found["dom"] = unique

    def settled():
        if _has_file_source(found) or found["mse"] or found["drm"]:
            return True
        # A blob: src alone is ambiguous. It may be a whole file or an MSE stream
        # whose first chunk hasn't arrived, so give the chunk a moment to show.
        return bool(found["dom"]) and time.time() > started + 8

    started = time.time()
    deadline = started + args.wait
    selectors = ([args.play_selector] if args.play_selector else []) + list(PLAY_SELECTORS)
    page.wait_for_timeout(min(3000, args.wait * 1000))
    scan()
    clicked = False
    while time.time() < deadline and not settled():
        for frame in page.frames:
            try:
                frame.evaluate(KICK)
            except PWError:
                pass
        if not clicked:
            for sel in selectors:
                for frame in page.frames:
                    try:
                        loc = frame.locator(sel).first
                        if loc.count() and loc.is_visible():
                            loc.click(timeout=3000)
                            log(f"Started the player: {sel}")
                            clicked = True
                            break
                    except PWError:
                        continue
                if clicked:
                    break
        page.wait_for_timeout(1500)
        scan()
    # Anything arriving just after the first source (a second track, the real
    # manifest behind a preroll request) still counts.
    if settled():
        page.wait_for_timeout(2000)
        scan()


def save_session(args, log) -> int:
    """Save the login held by an open browser, for later --storage-state runs.
    The person logs in themselves, including any PIN or 2FA; nothing here enters
    or sees a credential. The file holds live tokens, so it's private to its owner."""
    from playwright.sync_api import Error as PWError, sync_playwright
    if not args.cdp:
        log("ERROR: --save-session needs --cdp, the DevTools URL of the open browser.")
        return 2
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(args.cdp)
            ctx = browser.contexts[0] if browser.contexts else None
            state = ctx.storage_state() if ctx else {}
            # storage_state() misses local storage for tabs Playwright didn't
            # open itself, which is every tab here. Read it from them directly:
            # app-style sites (Plex Web, for one) keep their login there.
            origins = {o["origin"]: o for o in state.get("origins", [])}
            for page in (ctx.pages if ctx else []):
                try:
                    origin, items = page.evaluate(
                        "[location.origin, Object.entries(localStorage)"
                        ".map(([name, value]) => ({name, value}))]")
                except PWError:
                    continue
                if items and origin.startswith("http"):
                    origins[origin] = {"origin": origin, "localStorage": items}
            state["origins"] = list(origins.values())
    except PWError as e:
        log(f"ERROR: could not reach the browser at {args.cdp}: {str(e).splitlines()[0][:200]}")
        return 2
    if not (state.get("cookies") or state.get("origins")):
        log("ERROR: that browser holds no session to save. Log in first, then try again.")
        return 2
    path = Path(args.save_session)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f)
    log(f"Saved the session to {path} ({len(state.get('cookies', []))} cookies, "
        f"{len(state.get('origins', []))} site(s) of local storage).")
    return 0


def _play_through(page, found, args, log) -> None:
    """MSE only: the data exists only as the player fetches it, so the track has
    to actually play. Ends on endOfStream or the element finishing; stalling
    leaves found['finished'] False, and the caller discards the partial."""
    log("This player streams through Media Source: capturing in real time, "
        "so this takes as long as the track.")
    if args.rate != 1.0:
        page.evaluate("r => document.querySelectorAll('audio').forEach(a => a.playbackRate = r)", args.rate)
    deadline = time.time() + args.max_play
    last_note = time.time()
    while time.time() < deadline:
        page.wait_for_timeout(2000)
        if found["eos"] or page.evaluate(FINISHED):
            found["finished"] = True
            return
        newest = max((r["t"] for r in found["mse"].values()), default=time.time())
        if time.time() - newest > args.idle and not _playing(page):
            log(f"No new audio for {args.idle}s and the player has stopped.")
            return
        if time.time() - last_note > 60:
            mb = sum(r["bytes"] for r in found["mse"].values()) / 1e6
            log(f"  …{mb:.1f} MB captured so far")
            last_note = time.time()
    log(f"Reached the --max-play limit ({args.max_play}s).")


def _playing(page) -> bool:
    return page.evaluate("""() => [...document.querySelectorAll('audio'), ...(window.__encoreumEls || [])]
                            .some(a => !a.paused && !a.ended)""")


# --- planning + saving ------------------------------------------------------
def plan_items(found, args, archived: set) -> list[dict]:
    """Page order first (DOM), then anything only the network saw. Manifests are
    used only when there is no plain file, since a file needs no remux."""
    items, seen = [], set()
    page_title = clean_name(found["title"], args.title_max)

    def add(kind, ref, title="", ctype="", key=None):
        k = key or url_key(ref)
        if k in seen:
            return
        seen.add(k)
        stem = ""
        ext = ""
        if ref.startswith("http"):
            path = urllib.parse.urlsplit(ref).path
            stem = urllib.parse.unquote(Path(path).stem)
            ext = Path(path).suffix.lower()
            ext = ext if ext in AUDIO_EXTS else CTYPE_EXT.get(ctype, "")
        # A bare number ("3.mp3") says less than the page's title does.
        useful = stem if not re.fullmatch(r"[\W\d_]*", stem) else ""
        items.append({"kind": kind, "ref": ref, "key": k, "ext": ext, "ctype": ctype,
                      "title": clean_name(title or useful or page_title or "Untitled", args.title_max),
                      "archived": k in archived})

    for d in found["dom"]:
        if d["src"].startswith("http"):
            ctype = found["network"].get(d["src"], "")
            is_manifest = (d["src"] in found["manifests"]
                           or Path(urllib.parse.urlsplit(d["src"]).path).suffix.lower() in MANIFEST_EXTS)
            add("manifest" if is_manifest else "direct", d["src"], d["title"], ctype)
        elif d["src"].startswith("blob:") and not found["mse"] and not found["manifests"]:
            add("blob", d["src"], d["title"], key=hashlib.sha1(f"{args.url}|{len(items)}".encode()).hexdigest()[:16])
    for url, ctype in found["network"].items():
        add("direct", url, "", ctype)
    if not items:
        for url, ctype in found["manifests"].items():
            add("manifest", url, "", ctype)
    if not items:
        for sid, rec in sorted(found["mse"].items()):
            add("mse", str(sid), "", rec["mime"],
                key=hashlib.sha1(f"{args.url}|mse|{sid}".encode()).hexdigest()[:16])
    for n, item in enumerate(items, start=1):
        item["index"] = n
    return items


def final_name(item, ext, prefix) -> str:
    prefix = clean_name(prefix) or item["title"]
    return f"{prefix} - {item['index']:03d} - {item['title']}{ext}"


def save_direct(item, found, out, tmp, args, guard, log, headers) -> tuple[Path | None, str]:
    written = 0
    with fetch(item["ref"], headers, guard) as r, open(tmp, "wb") as f:
        ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
        item["ext"] = item["ext"] or CTYPE_EXT.get(ctype, "")
        while chunk := r.read(1 << 20):
            written += len(chunk)
            if written > args.max_bytes:
                return None, f"larger than the {args.max_bytes // 10**9} GB limit (--max-bytes)"
            f.write(chunk)
    return tmp, ""


def save_manifest(item, found, out, tmp, args, guard, log, headers) -> tuple[Path | None, str]:
    problem = manifest_problem(item["ref"], headers, guard)
    if problem:
        return None, f"not capturing this stream: {problem}"
    hdr = "".join(f"{k}: {v}\r\n" for k, v in headers.items() if k != "User-Agent")
    cmd = ["ffmpeg", "-y", "-v", "error", "-user_agent", headers.get("User-Agent", ""),
           "-protocol_whitelist", "http,https,tcp,tls,crypto", "-headers", hdr,
           "-i", item["ref"], "-map", "0:a:0", "-c", "copy", "-f", "matroska", str(tmp)]
    if subprocess.run(cmd, stderr=subprocess.DEVNULL).returncode != 0:
        return None, "ffmpeg could not read the stream"
    return tmp, ""


def save_blob(item, found, out, tmp, args, guard, log, headers) -> tuple[Path | None, str]:
    blob = found["blobs"].get(item["ref"])
    if not blob:
        return None, "could not read the audio from page memory"
    tmp.write_bytes(base64.b64decode(blob["data"]))
    item["ext"] = CTYPE_EXT.get((blob.get("type") or "").split(";")[0].lower(), "")
    return tmp, ""


def save_mse(item, found, out, tmp, args, guard, log, headers) -> tuple[Path | None, str]:
    if not found["finished"]:
        return None, ("the stream did not play to its end, so the capture is incomplete "
                      "and was discarded")
    src = out / f".mse-{item['ref']}.part"
    if not src.is_file():
        return None, "no audio was captured"
    os.replace(src, tmp)
    return tmp, ""


SAVERS = {"direct": save_direct, "manifest": save_manifest, "blob": save_blob, "mse": save_mse}


def save_item(item, found, args, guard, log, out: Path) -> Path | None:
    tmp = out / f".capture-{item['index']:03d}.part"
    headers = {"User-Agent": found["ua"] or "Mozilla/5.0", "Referer": args.url}
    if found["cookies"].get(item["ref"]):
        headers["Cookie"] = found["cookies"][item["ref"]]
    try:
        path, why = SAVERS[item["kind"]](item, found, out, tmp, args, guard, log, headers)
    except (OSError, urllib.error.URLError) as e:
        reason = getattr(e, "reason", None) or getattr(e, "code", None) or e
        path, why = None, f"download failed ({reason})"
    if not path:
        log(f"ERROR: {item['title']}: {why}")
        tmp.unlink(missing_ok=True)
        return None

    info = probe(path)
    if not info or info["duration"] <= 0 or info["has_video"]:
        what = "video, not audio" if info and info["has_video"] else "not playable audio"
        log(f"ERROR: {item['title']}: the server sent {what}, so it was discarded.")
        tmp.unlink(missing_ok=True)
        return None

    meta = {"album": args.album, "artist": args.artist, "album_artist": args.artist,
            "track": str(item["index"])} if (args.album or args.artist) else {}
    needs_container = item["kind"] != "direct" or not item["ext"]
    if needs_container:
        if info["codec"] not in CODEC_OUT:
            log(f"ERROR: {item['title']}: unsupported audio codec '{info['codec']}'.")
            tmp.unlink(missing_ok=True)
            return None
        item["ext"], muxer = CODEC_OUT[info["codec"]]
    else:
        muxer = EXT_MUXER.get(item["ext"], "")

    if needs_container or (meta and muxer):
        done = out / f".capture-{item['index']:03d}.out.part"
        if stream_copy(tmp, done, muxer, meta):
            os.replace(done, tmp)
        elif needs_container:
            log(f"ERROR: {item['title']}: could not package the {info['codec']} audio.")
            tmp.unlink(missing_ok=True)
            done.unlink(missing_ok=True)
            return None
        else:
            done.unlink(missing_ok=True)
            log(f"Note: could not write tags for {item['title']}; saved it untagged.")

    target = out / final_name(item, item["ext"], args.prefix or found["title"])
    os.replace(tmp, target)
    log(f"Saved {target.name} ({target.stat().st_size / 1e6:.1f} MB, "
        f"{info['duration'] / 60:.1f} min, {info['codec']})")
    if args.manifest:
        with open(args.manifest, "a", encoding="utf-8") as f:
            f.write(json.dumps({"key": item["key"], "file": target.name}) + "\n")
    return target


def read_archive(path) -> set:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, TypeError):
        return set()
    return {ln.split()[1] for ln in lines if ln.startswith(ARCHIVE_PREFIX + " ") and len(ln.split()) > 1}


def explain_nothing(found, log) -> None:
    if found.get("error"):
        log(f"ERROR: could not open the page: {found['error']}")
    elif found["status"] >= 400:
        log(f"ERROR: the page answered HTTP {found['status']}. If that is a login or a bot "
            "check, this tool does not work around those.")
    elif found["blocked"] and not found["dom"]:
        log("ERROR: no audio found, and the page tried private addresses that were blocked: "
            f"{', '.join(sorted(found['blocked'])[:3])}. Name a trusted host with --allow-host.")
    else:
        log("ERROR: no audio found on this page. It may need a login (--storage-state), or a "
            "specific button pressed first (--play-selector).")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Save the audio a web page's own player streams.")
    ap.add_argument("url")
    ap.add_argument("--out", type=Path, help="folder to save into (required unless --list)")
    ap.add_argument("--list", action="store_true", help="report what would be saved; save nothing")
    ap.add_argument("--json", action="store_true", help="with --list: print the result as JSON")
    ap.add_argument("--items", default="", help="only these item numbers, e.g. 1,3")
    ap.add_argument("--prefix", default="", help="filename prefix (default: the page title)")
    ap.add_argument("--album", default="", help="write this album tag")
    ap.add_argument("--artist", default="", help="write this artist tag")
    ap.add_argument("--title-max", type=int, default=80, help="longest title in a filename")
    ap.add_argument("--archive", help="yt-dlp-style archive to skip already-saved items (read only)")
    ap.add_argument("--manifest", help="append one JSON line per saved file here")
    ap.add_argument("--wait", type=int, default=25, help="seconds to wait for audio to appear")
    ap.add_argument("--play-selector", default="", help="CSS selector of the page's Play button")
    ap.add_argument("--storage-state", default="", help="Playwright storage-state JSON: a logged-in session")
    ap.add_argument("--cdp", default="", help="attach to an open browser at this DevTools URL")
    ap.add_argument("--save-session", default="", help="with --cdp: save that browser's login to this file and exit")
    ap.add_argument("--allow-host", action="append", default=[],
                    help="host allowed despite a private address; '.example.com' matches subdomains")
    ap.add_argument("--no-sandbox", action="store_true", help="for containers that can't run Chromium's sandbox")
    ap.add_argument("--sleep", type=float, default=3.0, help="seconds to pause between files")
    ap.add_argument("--max-bytes", type=int, default=4_000_000_000, help="largest file to accept")
    ap.add_argument("--max-play", type=int, default=6 * 3600, help="longest real-time capture, seconds")
    ap.add_argument("--idle", type=int, default=120, help="give up after this long with no new audio")
    ap.add_argument("--rate", type=float, default=1.0, help="playback speed during a real-time capture")
    args = ap.parse_args(argv)
    if not args.list and not args.out and not args.save_session:
        ap.error("--out is required unless --list or --save-session is given")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    log = Log(to_stderr=args.json)
    try:
        import playwright  # noqa: F401
    except ImportError:
        log("ERROR: page capture needs Playwright: pip install playwright")
        return 3
    if args.save_session:
        return save_session(args, log)
    if not args.cdp and not find_chromium():
        log("Note: no system Chromium found; trying Playwright's own "
            "(install with `playwright install chromium`).")

    guard = Guard(args.allow_host)
    if guard.blocked(args.url):
        log(f"ERROR: refusing {safe_url(args.url)}: not http(s), or a private address. "
            "If this is your own server, name it with --allow-host.")
        return 2
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)

    log(f"Opening {safe_url(args.url)}")
    from playwright.sync_api import Error as PWError
    try:
        found = inspect_page(args, guard, log, None if args.list else args.out)
    except PWError as e:
        first = str(e).splitlines()[0][:200]
        if "launch" in first.lower() and not args.no_sandbox:
            log("ERROR: Chromium would not start, most likely because its sandbox can't run "
                "here (usual inside Docker). Use --no-sandbox, or CHROMIUM_NO_SANDBOX=1 in "
                "Encoreum; the README explains the trade-off.")
        else:
            log(f"ERROR: the browser failed: {first}")
        return 2
    drm = sorted(set(found["drm"]))
    items = plan_items(found, args, read_archive(args.archive))
    wanted = {int(x) for x in args.items.split(",") if x.strip().isdigit()}

    if args.json:
        print(json.dumps({
            "title": found["title"], "drm": drm, "http_status": found["status"],
            "error": found.get("error", ""),
            "blocked": sorted(found["blocked"])[:5],
            "items": [{k: i[k] for k in ("index", "kind", "key", "title", "ext", "archived")}
                      | {"source": safe_url(i["ref"]) if i["ref"].startswith("http") else i["kind"]}
                      for i in items]}))
        return 0 if items else 2

    if drm and not any(i["kind"] in ("direct", "manifest") for i in items):
        log(f"ERROR: this player uses DRM ({', '.join(drm)}). DRM-protected audio is not captured.")
        return 2
    if drm:
        log(f"Note: the page also requested DRM ({', '.join(drm)}); it may use it for other media.")
    items = [i for i in items if i["kind"] != "mse" or not drm]
    if not items:
        explain_nothing(found, log)
        return 2

    log(f"Found {len(items)} audio source(s) on “{clean_name(found['title'])}”.")
    for i in items:
        src = safe_url(i["ref"]) if i["ref"].startswith("http") else "(in page memory)"
        log(f"  {i['index']:>3}. [{i['kind']}] {i['title']}  {src}")
    if args.list:
        return 0

    failed, saved = 0, 0
    todo = [i for i in items if (not wanted or i["index"] in wanted)]
    for n, item in enumerate(todo):
        if item["archived"]:
            log(f"Already saved earlier, skipping: {item['title']}")
            continue
        if n and saved and args.sleep:
            time.sleep(args.sleep)
        if save_item(item, found, args, guard, log, args.out):
            saved += 1
        else:
            failed += 1
    for leftover in args.out.glob(".mse-*.part"):
        leftover.unlink(missing_ok=True)
    log(f"Done: {saved} saved, {failed} failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
