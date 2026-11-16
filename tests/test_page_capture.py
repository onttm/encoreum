#!/usr/bin/env python3
"""
End-to-end tests for scripts/capture-page-audio.py, against a local fixture site
that imitates the ways real players deliver audio. Needs no internet.

    docker run --rm -v "$PWD/tests:/tests:ro" --entrypoint python3 encoreum:local \
        /tests/test_page_capture.py

Stdlib plus ffmpeg and the script's own requirements.
"""
import http.server
import itertools
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

SCRIPT = Path(os.environ.get("CAPTURE_SCRIPT", "/app/scripts/capture-page-audio.py"))
WORK = Path(tempfile.mkdtemp(prefix="capture-test-"))
MEDIA = WORK / "media"
MEDIA.mkdir()
SECONDS = 6


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={SECONDS}", *args], check=True)


ffmpeg(str(MEDIA / "chapter.mp3"))
ffmpeg("-c:a", "aac", "-movflags", "frag_keyframe+empty_moov+default_base_moof",
       "-frag_duration", "1000000", str(MEDIA / "frag.m4a"))
ffmpeg("-c:a", "aac", "-f", "hls", "-hls_time", "2", "-hls_list_size", "0",
       "-hls_segment_filename", str(MEDIA / "seg%d.ts"), str(MEDIA / "plain.m3u8"))
(MEDIA / "drm.m3u8").write_text(
    '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k.bin"\n#EXTINF:2,\nseg0.ts\n#EXT-X-ENDLIST\n')
(MEDIA / "drm.mpd").write_text(
    '<MPD><Period><AdaptationSet><ContentProtection schemeIdUri="urn:x"/></AdaptationSet></Period></MPD>')

SECRET = "s3cr3t-token-value"
PAGES = {
    "direct": '<title>Direct Page</title><audio src="/media/chapter.mp3" controls></audio>',
    "two": '<title>Two Chapters</title><audio src="/media/chapter.mp3"></audio>'
           '<audio title="Second One" src="/media/frag.m4a"></audio>',
    "source": '<title>Source Tag</title><audio controls><source src="/media/chapter.mp3"></audio>',
    "token": f'<title>Tokened</title><audio src="/media/chapter.mp3?X-Plex-Token={SECRET}"></audio>',
    "cookie": '<title>Needs Cookie</title><audio src="/gated/chapter.mp3"></audio>',
    "button": """<title>Click To Play</title><button aria-label="Play" onclick="
        const a=document.createElement('audio');a.src='/media/chapter.mp3';document.body.append(a);a.play()">go</button>""",
    "unattached": """<title>Unattached</title><script>
        window.onload=()=>{new Audio('/media/chapter.mp3').play().catch(()=>{})}</script>""",
    "hls": '<title>HLS Page</title><audio src="/media/plain.m3u8"></audio>',
    "hls-drm": '<title>HLS DRM</title><audio src="/media/drm.m3u8"></audio>',
    "dash-drm": '<title>DASH DRM</title><audio src="/media/drm.mpd"></audio>',
    "blob": """<title>Blob Page</title><audio id=a></audio><script>
        fetch('/hidden/chapter').then(r=>r.blob()).then(b=>{a.src=URL.createObjectURL(b)})</script>""",
    "mse": """<title>MSE Page</title><audio id=a autoplay muted></audio><script>
        const ms=new MediaSource();a.src=URL.createObjectURL(ms);
        ms.addEventListener('sourceopen',async()=>{
          const sb=ms.addSourceBuffer('audio/mp4; codecs="mp4a.40.2"');
          const d=new Uint8Array(await (await fetch('/hidden/frag')).arrayBuffer());
          const n=Math.ceil(d.length/4);
          for(let i=0;i<d.length;i+=n){
            sb.appendBuffer(d.subarray(i,i+n));
            await new Promise(r=>sb.addEventListener('updateend',r,{once:true}));
          }
          ms.endOfStream();
        });</script>""",
    "eme": """<title>EME Page</title><audio id=a></audio><script>
        const ms=new MediaSource();a.src=URL.createObjectURL(ms);
        navigator.requestMediaKeySystemAccess('com.widevine.alpha',
          [{audioCapabilities:[{contentType:'audio/mp4; codecs="mp4a.40.2"'}]}]).catch(()=>{});
        </script>""",
    "video": '<title>Video Only</title><video src="/media/clip.mp4" autoplay muted></video>',
    "fake": '<title>Fake Audio</title><audio src="/fake/chapter.mp3"></audio>',
    "nothing": "<title>No Player</title><p>Just text.</p>",
    "forbidden": "BLOCKED",
    "private": '<title>Private</title><audio src="http://10.255.255.1/x.mp3"></audio>',
}


class Site(http.server.SimpleHTTPRequestHandler):
    log_message = lambda *a: None

    def do_GET(self):
        path = self.path.split("?")[0]
        name = path.strip("/")
        if name == "forbidden":
            return self._send(403, "text/html", b"blocked")
        if name in PAGES:
            return self._send(200, "text/html", PAGES[name].encode())
        if path == "/gated/chapter.mp3":
            if "session=ok" not in (self.headers.get("Cookie") or "") or "/cookie" not in (self.headers.get("Referer") or ""):
                return self._send(401, "text/plain", b"no")
            return self._send(200, "audio/mpeg", (MEDIA / "chapter.mp3").read_bytes())
        if path == "/hidden/chapter":
            return self._send(200, "application/x-custom", (MEDIA / "chapter.mp3").read_bytes())
        if path == "/hidden/frag":
            return self._send(200, "application/x-custom", (MEDIA / "frag.m4a").read_bytes())
        if path == "/fake/chapter.mp3":
            return self._send(200, "audio/mpeg", b"<html>this is an error page, not audio</html>")
        if path.startswith("/media/"):
            f = MEDIA / path.split("/")[-1]
            if f.is_file():
                ctype = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".m3u8": "application/vnd.apple.mpegurl",
                         ".mpd": "application/dash+xml", ".ts": "video/mp2t", ".mp4": "video/mp4"}.get(f.suffix, "application/octet-stream")
                return self._send(200, ctype, f.read_bytes())
        return self._send(404, "text/plain", b"nope")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if self.path.split("?")[0].strip("/") == "cookie":
            self.send_header("Set-Cookie", "session=ok; Path=/")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


server = Server(("127.0.0.1", 0), Site)
PORT = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"
results = []


runs = itertools.count()


def capture(page, *extra, allow=True, listing=False):
    out = WORK / f"out-{page}-{next(runs)}"
    cmd = ["python3", "-I", str(SCRIPT), f"{BASE}/{page}", "--no-sandbox", "--sleep", "0", "--wait", "10"]
    if allow:
        cmd += ["--allow-host", "127.0.0.1"]
    cmd += ["--list"] if listing else ["--out", str(out)]
    cmd += list(extra)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout + r.stderr, out


def files(out):
    return sorted(p.name for p in out.glob("*")) if out.is_dir() else []


def duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    return float(r.stdout.strip() or 0)


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n      {detail[-900:]}"), flush=True)


def expect_saved(name, page, pattern, *extra):
    rc, log, out = capture(page, *extra)
    names = files(out)
    good = rc == 0 and len(names) == 1 and re.search(pattern, names[0])
    if good:
        good = abs(duration(out / names[0]) - SECONDS) < 1.0
    check(name, bool(good), f"rc={rc} files={names}\n{log}")
    return out


def expect_refused(name, page, *needles, rc_want=(1, 2), **kw):
    rc, log, out = capture(page, **kw)
    ok = rc in rc_want and not files(out) and all(n.lower() in log.lower() for n in needles)
    check(name, ok, f"rc={rc} files={files(out)}\n{log}")


# --- each way a player can deliver audio
expect_saved("direct <audio src>", "direct", r"^Direct Page - 001 - chapter\.mp3$")
expect_saved("<source> child", "source", r"\.mp3$")
expect_saved("cookie + Referer carried to the download", "cookie", r"\.mp3$")
expect_saved("element created by the page's Play button", "button", r"\.mp3$")
expect_saved("unattached new Audio(), seen on the network", "unattached", r"\.mp3$")
expect_saved("plain HLS, remuxed with no re-encode", "hls", r"\.m4a$")
expect_saved("blob: holding a whole file", "blob", r"\.mp3$")
expect_saved("Media Source chunks, played through", "mse", r"\.m4a$")

rc, log, out = capture("two", "--prefix", "Book")
check("several players keep page order and numbering",
      rc == 0 and [re.sub(r"\.\w+$", "", n) for n in files(out)] == ["Book - 001 - chapter", "Book - 002 - Second One"],
      f"{files(out)}\n{log}")

rc, log, out = capture("direct", "--album", "My Album", "--artist", "An Author")
tags = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json",
                       str(out / files(out)[0])], capture_output=True, text=True).stdout if files(out) else ""
check("album and artist tags written", "My Album" in tags and "An Author" in tags, tags + log)

# --- refusals
expect_refused("HLS with an encryption key is refused", "hls-drm", "encrypted")
expect_refused("DASH with ContentProtection is refused", "dash-drm", "content protection")
expect_refused("a page asking for Widevine is refused", "eme", "DRM")
expect_refused("a video-only page yields nothing", "video", "no audio found")
expect_refused("an HTML error page isn't published as audio", "fake", "not playable audio")
expect_refused("a page with no player says so", "nothing", "no audio found")
expect_refused("HTTP 403 is reported, not retried around", "forbidden", "HTTP 403")
expect_refused("a private page is refused without --allow-host", "direct", "private address", allow=False)

# --- privacy of logs
rc, log, _ = capture("token")
check("stream tokens never reach the log", rc == 0 and SECRET not in log, log)
rc, log, _ = capture("token", listing=True, )
check("stream tokens never reach the listing", rc == 0 and SECRET not in log, log)
rc, log, _ = capture("token", "--json", listing=True)
check("stream tokens never reach the JSON", rc == 0 and SECRET not in log, log)

# --- listing and resume
rc, log, out = capture("direct", listing=True)
check("--list saves nothing", rc == 0 and not out.exists(), log)

first = WORK / "manifest.jsonl"
rc, log, out = capture("direct", "--manifest", str(first))
key = json.loads(first.read_text().splitlines()[0])["key"]
archive = WORK / "archive.txt"
archive.write_text(f"youtube abc\npagecapture {key}\n")
rc, log, out = capture("direct", "--archive", str(archive))
check("an archived item is skipped", rc == 0 and not files(out) and "Already saved" in log, f"{files(out)}\n{log}")

rc, log, out = capture("two", "--items", "2")
check("--items picks by number and keeps it", rc == 0 and len(files(out)) == 1 and " - 002 - " in files(out)[0], f"{files(out)}\n{log}")

leftovers = [p.name for p in WORK.glob("out-*/*") if p.name.endswith(".part")]
check("no .part files left behind", not leftovers, str(leftovers))

passed, total = sum(results), len(results)
print(f"\n{passed} of {total} passed")
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(0 if passed == total else 1)
