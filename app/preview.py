"""
preview.py -- dry-run: compute the file list a job *would* create, no download.

Crucially this mirrors the WHOLE pipeline, including the footage-date rename:
when "Rename by footage date" is on (the default), the real run renames every
file to  Season <concert-year>/<Show> - S<year>E<mmdd> - <title>.mkv  using the
performance date parsed from each video's title/description. So the preview must
show those names, not the raw yt-dlp upload-date names. To stay a single source
of truth it imports the exact date parser from scripts/rename-by-footage-date.py.

Two extraction strategies:
  * episode mode WITHOUT rename -> one fast `--flat-playlist` call (keeps the
    exact E001.. numbering from playlist order).
  * everything else -> `--print` a compact JSON of {title, description, dates}
    for the first PREVIEW_CAP videos and compute the final names locally.

Also exposes SUPPORTED_BROWSERS for validating --cookies-from-browser.
"""
import difflib
import importlib.util
import json
import os
import re
import signal
import subprocess
from collections import defaultdict
from pathlib import Path

from . import config, library, pipeline, presets

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

# Reuse the real footage-date parser (find_date / date_from_meta / clean) so the
# preview and the actual rename can never drift apart.
_spec = importlib.util.spec_from_file_location(
    "cdl_rename_footage", str(SCRIPTS / "rename-by-footage-date.py"))
_R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_R)

# Reuse the performance-name builder (parse_location / extract_tour / build_core)
# so the preview and the real rename can never drift apart.
_spec_perf = importlib.util.spec_from_file_location(
    "cdl_name_performance", str(SCRIPTS / "name-by-performance.py"))
_P = importlib.util.module_from_spec(_spec_perf)
_spec_perf.loader.exec_module(_P)

SUPPORTED_BROWSERS = {
    "brave", "chrome", "chromium", "edge", "firefox", "opera", "safari",
    "vivaldi", "whale",
}

PREVIEW_CAP = 25            # full-extract at most this many videos for the names
_SEP = "\x1f"
_FIELDS = ("%(.{id,title,description,upload_date,release_date,playlist_index,"
           "height,vcodec,acodec,abr,asr})j")


def _fmt_sort(p: dict) -> str:
    return ("res:1080,vcodec:h264,acodec:aac,fps" if p.get("quality") == "compatible"
            else "res,fps,hdr:12,vcodec,channels,acodec")


def _vcodec_short(v: str) -> str:
    v = (v or "none").lower()
    for pfx, name in (("av01", "AV1"), ("vp9", "VP9"), ("vp09", "VP9"),
                      ("avc", "H.264"), ("h264", "H.264"), ("hev", "HEVC"),
                      ("hvc", "HEVC"), ("vp8", "VP8")):
        if v.startswith(pfx):
            return name
    return "265" in v and "HEVC" or (v.split(".")[0] if v != "none" else "")


def _acodec_short(a: str) -> str:
    a = (a or "none").lower()
    if a.startswith("opus"):
        return "Opus"
    if a.startswith("mp4a") or "aac" in a:
        return "AAC"
    if a.startswith("ac-3") or a == "ac3":
        return "AC3"
    if "mp3" in a:
        return "MP3"
    return "" if a == "none" else a.split(".")[0]


_CODEC_RANK = {"AV1": 4, "VP9": 3, "HEVC": 2, "H.264": 1, "VP8": 0}


def probe_compat(url: str, ck: list) -> dict | None:
    """
    Inspect the first video's full format list and summarise what codecs/
    resolutions are available + a Plex-compatibility recommendation. Used by the
    dry run to answer "will this Direct Play, or will the server transcode?".
    """
    try:
        r = _ytdlp([*ck, "--simulate", "-j", "--playlist-items", "1", url], timeout=90)
    except subprocess.TimeoutExpired:
        return None
    line = next((ln for ln in r.stdout.splitlines() if ln.lstrip().startswith("{")), "")
    if not line:
        if "drm" in (r.stderr or "").lower():
            return {"drm": True, "summary": "⚠ DRM-protected — this can't be downloaded."}
        return None
    try:
        info = json.loads(line)
    except ValueError:
        return None

    drm = bool(info.get("_has_drm"))
    vmax: dict = {}   # codec -> max height
    audio: dict = {}  # codec -> max abr
    for f in (info.get("formats") or []):
        if f.get("has_drm"):
            drm = True
        vc, h = f.get("vcodec"), f.get("height") or 0
        if vc and vc != "none":
            name = _vcodec_short(vc)
            if name:
                vmax[name] = max(vmax.get(name, 0), int(h))
        ac, abr = f.get("acodec"), f.get("abr") or 0
        if ac and ac != "none":
            a = _acodec_short(ac)
            if a:
                audio[a] = max(audio.get(a, 0), abr)

    if drm or not vmax:
        return {"drm": True, "summary": "⚠ DRM-protected — this can't be downloaded."} if drm else None

    max_h = max(vmax.values())
    best = max(vmax.items(), key=lambda kv: (kv[1], _CODEC_RANK.get(kv[0], 0)))[0]
    h264 = vmax.get("H.264", 0)
    parts = [f"Best: {best} {max_h}p"]
    if h264 >= max_h and h264:
        parts.append(f"H.264 {h264}p available → Direct Plays on virtually any client")
    elif h264:
        parts.append(f"H.264 caps at {h264}p (above that it's {best} → server transcodes for older / Apple-TV clients)")
    else:
        parts.append(f"no H.264 → {best} only (server transcodes for non-{best} clients)")
    if audio:
        parts.append("Audio: " + ", ".join(sorted(audio)))
    return {"drm": False, "max_height": max_h, "best": best, "h264_max": h264,
            "video": vmax, "audio": sorted(audio), "recommend_compatible": bool(h264),
            "summary": " · ".join(parts)}


def _media_str(info: dict) -> str:
    parts = []
    h = info.get("height")
    if h:
        parts.append(f"{int(h)}p")
    vc = _vcodec_short(info.get("vcodec"))
    if vc:
        parts.append(vc)
    ac = _acodec_short(info.get("acodec"))
    abr = info.get("abr")
    audio = (ac + (f" ~{round(abr)}k" if abr else "")).strip()
    if audio:
        parts.append(audio)
    return " · ".join(parts)


def _cookie_args(p: dict) -> list:
    b = (p.get("cookies_browser") or "").strip()
    return ["--cookies-from-browser", b] if b else []


def _na(s: str) -> str:
    """yt-dlp prints the literal string "NA" for a template field that has no
    value (e.g. %(playlist_title)s on a bare video URL, not a playlist) --
    normalise that sentinel to a real empty string everywhere it's parsed."""
    s = (s or "").strip()
    return "" if s.upper() == "NA" else s


_BRACKETED = re.compile(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}")
_LEAD_YEAR = re.compile(r"^\s*\d{4}\s*-\s*")


def _norm_for_match(name: str) -> str:
    """Strip bracketed metadata ("[Author]", "(Narrator)", "{287mb}") and a
    leading "YYYY - " prefix before fuzzy-matching -- both are extremely
    common media-library conventions (this app's own performance/sequence
    naming uses the same year-prefix shape), so a decorated real folder name
    can still match a plain candidate string on its core title."""
    s = _BRACKETED.sub(" ", name)
    s = _LEAD_YEAR.sub("", s)
    return " ".join(s.split()).strip(" -").lower()


def _clean_show(name: str) -> str:
    """Tidy a channel name into a likely show/artist name."""
    name = (name or "").strip()
    for suf in (" - Topic", "- Topic"):
        if name.lower().endswith(suf.lower()):
            name = name[: -len(suf)].strip()
    return name


def suggest_show(url: str, cookies_browser: str = "") -> dict:
    """Guess a show/artist name from a URL's channel + playlist title (cheap,
    flat-playlist -- one video, no description). Used both by the live
    fill-while-typing autofill (/api/suggest-show) and by the richer preview
    suggestion chips (_show_suggestions), so there is one signal source."""
    empty = {"suggestion": "", "channel": "", "uploader": "", "playlist_title": ""}
    url = (url or "").strip()
    if not url:
        return empty
    ck = _cookie_args({"cookies_browser": cookies_browser})
    try:
        r = _ytdlp([*ck, "--flat-playlist", "--playlist-items", "1",
                    "--print", f"%(channel)s{_SEP}%(uploader)s{_SEP}%(playlist_title)s", url],
                   timeout=60)
    except subprocess.TimeoutExpired:
        return empty
    line = next((ln for ln in r.stdout.splitlines() if ln.strip()), "")
    parts = line.split(_SEP, 2)
    channel = _na(parts[0]) if len(parts) > 0 else ""
    uploader = _na(parts[1]) if len(parts) > 1 else ""
    playlist_title = _na(parts[2]) if len(parts) > 2 else ""
    return {"suggestion": _clean_show(channel or uploader),
            "channel": channel, "uploader": uploader, "playlist_title": playlist_title}


_AUDIOBOOK_SIGNAL = re.compile(r"\baudiobook\b|\bunabridged\b|\bnarrated by\b|\bread by\b", re.I)
_PODCAST_SIGNAL = re.compile(r"\bpodcast\b", re.I)


def _suggest_preset(info: dict, naming: str) -> dict | None:
    sugg = _suggest_preset_any(info, naming)
    # Never nudge toward an add-on that's switched off (presets.ADDON_NAMES).
    if sugg and presets.is_addon(sugg["value"]):
        from . import settings
        if sugg["value"] not in settings.addons_enabled():
            return None
    return sugg


def _suggest_preset_any(info: dict, naming: str) -> dict | None:
    """A conservative, OPT-IN content-type nudge -- fires only on strong,
    low-ambiguity keyword signals in the channel/playlist title, and says
    NOTHING when unsure. Deliberately narrow:

    - No "this sounds live/a concert" -> Music suggestion: "Live at <venue>"
      appears in the title of a huge share of ordinary CONCERT VIDEOS this app
      is built to download as video (the default, primary use case) -- firing
      on that phrase would make the suggestion noise on most normal downloads,
      the exact "review queue full of junk" trap this app already learned
      from setlist grading (see captures.py). A parsed venue is evidence of
      "this is a live performance", not evidence of "you wanted AUDIO ONLY".
    - No description/per-video fetch -- same free signal already fetched for
      the show-name chips (channel + playlist title), so this costs nothing
      extra. This app's own CLAUDE.md documents that naive channel/title
      classification goes wrong easily (Coachella/Lollapalooza, HANORAH) --
      the fix there was negative markers first + deep sampling, neither of
      which is affordable for a live per-preview nudge. Staying conservative
      here is the equivalent move: say nothing rather than guess confidently.
    """
    text = f"{info.get('channel') or ''} {info.get('uploader') or ''} {info.get('playlist_title') or ''}"
    if naming != "audiobook" and _AUDIOBOOK_SIGNAL.search(text):
        return {"value": "audiobook", "label": presets.label("audiobook"),
                "reason": "the channel/playlist title mentions an audiobook",
                "confidence": "probable"}
    if naming != "podcast" and _PODCAST_SIGNAL.search(text):
        return {"value": "podcast", "label": presets.label("podcast"),
                "reason": "the channel/playlist title mentions a podcast",
                "confidence": "weak"}
    return None


_AUDIOBOOK_TITLE_JUNK = re.compile(
    r"[|\-–—:]?\s*\(?\b(full\s+)?(audiobook|unabridged|abridged)\b\)?\s*", re.I)


def _clean_audiobook_title(title: str) -> str:
    """Strip "Full Audiobook"/"(Unabridged)"-type decoration so the Title
    suggestion is closer to the book's actual name, not the upload label."""
    cleaned = _AUDIOBOOK_TITLE_JUNK.sub(" ", title)
    return " ".join(cleaned.split()).strip(" -–—:|")


def _suggest_author(info: dict, naming: str) -> dict | None:
    """Audiobook only: the channel is very often literally an author/narrator
    channel ("GreatAudiobooks"), so it's offered as an AUTHOR suggestion
    instead of a Title one here -- see _show_suggestions, which skips the
    channel candidate for audiobook for exactly this reason. Every other
    preset has no Author concept, so this is always None for them (the
    suggestions.metadata contract stays {} there, as documented)."""
    if naming != "audiobook":
        return None
    channel = _clean_show(info.get("channel") or info.get("uploader") or "")
    if not channel:
        return None
    return {"value": channel, "label": "From the channel name", "confidence": "probable"}


def _show_suggestions(info: dict, dest: str, naming: str = "") -> list:
    """Candidate TITLE/show names for the preview's suggestion chips: the
    channel name, the cleaned playlist title, and a fuzzy match against
    folders that ALREADY EXIST under this destination.

    The existing-folder match is surfaced first and wins over the other two on
    a value collision, precisely to steer toward reusing a folder rather than
    forking a near-duplicate one -- this app's own CLAUDE.md documents that
    exact failure mode (Band C/Season 2019 vs 2023 vs 2024 vs 2025, the product
    of inconsistent naming across separate downloads of the same show).

    confidence uses this app's existing certain/probable/weak vocabulary
    (see captures.py's setlist grading) rather than a fabricated score.
    """
    is_audiobook = naming == "audiobook"
    channel = _clean_show(info.get("channel") or info.get("uploader") or "")
    title = (info.get("playlist_title") or "").strip()
    if is_audiobook and title:
        title = _clean_audiobook_title(title)

    out = []
    seen = set()

    def add(kind, value, label, confidence):
        value = (value or "").strip()
        key = value.lower()
        if not value or key in seen:
            return
        seen.add(key)
        out.append({"kind": kind, "value": value, "label": label, "confidence": confidence})

    # For audiobook, the channel is offered as Author (_suggest_author) INSTEAD
    # of Title -- a narrator/publisher channel name is rarely the book's title.
    if channel and not is_audiobook:
        add("channel", channel, "From the channel name", "probable")
    if title and not _P._TOUR_JUNK.search(title):
        add("title", title, "From the playlist title", "probable")

    try:
        existing = [e["name"] for e in library.browse(dest).get("entries", [])]
    except ValueError:
        existing = []
    best_ratio, best_name = 0.0, None
    for candidate in [c["value"] for c in out]:
        for name in existing:
            # Compare NORMALIZED forms: real library folders are routinely
            # decorated with bracketed metadata ("Title [Author] (Narrator)")
            # that a clean candidate string will never contain, which tanks a
            # raw-string ratio even for an obvious match (measured: 0.55 raw
            # vs 0.67 normalized for a real audiobook folder in this library).
            ratio = difflib.SequenceMatcher(
                None, _norm_for_match(candidate), _norm_for_match(name)).ratio()
            if ratio > best_ratio:
                best_ratio, best_name = ratio, name
    if best_name and best_ratio >= 0.6:
        confidence = "certain" if best_ratio >= 0.92 else "probable"
        out[:] = [o for o in out if o["value"].lower() != best_name.lower()]
        out.insert(0, {
            "kind": "existing", "value": best_name,
            "label": f"Use existing folder — already in {dest}/",
            "confidence": confidence,
        })
    return out


def _ytdlp(args, timeout):
    return subprocess.run(
        [config.YTDLP_BIN, "--no-warnings", "--ignore-errors", *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _rel(path: str) -> str:
    try:
        return str(Path(path).relative_to(config.LIBRARY_ROOT))
    except ValueError:
        return path


def _flat_count(p, ck) -> int:
    r = _ytdlp([*ck, "--flat-playlist", "--print", "%(id)s", p["url"]], timeout=120)
    return len([ln for ln in r.stdout.splitlines() if ln.strip()])


def flat_count(url: str, cookies_browser: str = "") -> int:
    """Public wrapper around _flat_count for callers outside this module --
    main.py uses this to decide whether a playlist is large enough to chunk
    before a job even starts (see main.py's /jobs handler)."""
    return _flat_count({"url": url}, _cookie_args({"cookies_browser": cookies_browser}))


def preview(p: dict, show_dir: Path) -> dict:
    naming = p.get("naming", "datecode")
    run_rename = bool(p.get("run_rename"))
    ck = _cookie_args(p)
    if p.get("page_capture"):
        return _preview_page_capture(p, show_dir)
    try:
        if naming == "performance":
            res = _preview_performance(p, show_dir, ck)
        elif naming == "sequence":
            res = _preview_sequence(p, show_dir, ck)
        elif naming == "episode" and not run_rename:
            res = _preview_episode(p, show_dir, ck)   # fast path, exact numbering
        elif naming in presets.AUDIO_NAMES:
            res = _preview_flat_audio(p, show_dir, ck)
        else:
            res = _preview_meta(p, show_dir, ck, run_rename)
    except subprocess.TimeoutExpired:
        return {"error": "Timed out reading the playlist (it may be very large). "
                         "Try a single video or a smaller list."}
    if isinstance(res, dict) and "error" not in res:
        # The compat probe answers "will Plex Direct Play this?" -- a
        # video-library question that doesn't apply to an audio-only
        # extraction, whether that's an audio preset OR the plain "Audio
        # only" checkbox on an otherwise-video naming mode.
        if naming not in presets.AUDIO_NAMES and not p.get("audio_only"):
            res["compat"] = probe_compat(p["url"], ck)
        # One shared flat-playlist call feeds both suggestion kinds below --
        # show-name chips AND the preset nudge -- so this costs nothing extra
        # over the show-name-only version.
        info = suggest_show(p["url"], p.get("cookies_browser", ""))
        # Show-name suggestion chips: preset-agnostic, always computed -- even
        # when Show is already filled, so the chips stay pickable/switchable.
        author_sugg = _suggest_author(info, naming)
        res["suggestions"] = {
            "show": _show_suggestions(info, p.get("destination", ""), naming),
            "preset": _suggest_preset(info, naming),
            "metadata": {"author": author_sugg} if author_sugg else {},
        }
        # The author value THIS preview's paths were actually rendered with
        # (may be the unknown-author placeholder) -- same live-rewrite trick
        # as "show" below, for the Author segment of the audiobook folder path.
        res["author"] = p.get("author", "")
        # Same mode-mixing check the real job applies (pipeline.layout_conflict):
        # say so BEFORE the download, not only in the job log afterwards.
        conflict = pipeline.layout_conflict(naming, run_rename, pipeline.existing_layout(show_dir))
        if conflict:
            res["note"] = ("Heads up: " + conflict.replace("so that step was skipped",
                           "so that step will be skipped").replace("were added", "will be added")
                           + (" " + res["note"] if res.get("note") else ""))
        # The show value THIS preview's paths were actually rendered with (may
        # be the untitled placeholder) -- lets the client rewrite already-
        # rendered paths in place when a suggestion chip is picked, with no
        # second round trip.
        res["show"] = p.get("show", "")
    return res


def _preview_page_capture(p: dict, show_dir: Path) -> dict:
    """Dry run for page audio capture: open the page, report what the player uses,
    save nothing. Runs the same command as the real job (pipeline.page_capture_cmd).
    Its own process group, because a timeout has to take Chromium down too --
    killing only the Python parent would leave a browser running per preview."""
    proc = subprocess.Popen(pipeline.page_capture_cmd(p) + ["--list", "--json"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        return {"error": "Timed out opening that page."}
    try:
        res = json.loads(out)
    except ValueError:
        return {"error": (err.strip().splitlines() or ["Could not read that page."])[-1]}

    if res["drm"] and not any(i["kind"] in ("direct", "manifest") for i in res["items"]):
        return {"error": "This player uses DRM, so its audio can't be captured."}
    if res["error"]:
        return {"error": f"Could not open the page: {res['error']}"}
    if res["http_status"] >= 400:
        return {"error": f"The page answered HTTP {res['http_status']}. If that's a login, "
                         "save a session with scripts/page-login.sh. Bot checks aren't worked around."}
    if not res["items"]:
        hint = (f" It tried private addresses that were blocked: {', '.join(res['blocked'][:2])}."
                if res["blocked"] else " It may need a login: see scripts/page-login.sh.")
        return {"error": "No audio found on this page." + hint}

    show = p["show"]
    existing_max = pipeline.existing_max_seq(show_dir)
    include = set(p.get("include") or [])
    rows = [i for i in res["items"] if not i["archived"] and (not include or i["index"] in include)]
    shift = existing_max if rows and min(i["index"] for i in rows) <= existing_max else 0
    items = []
    for i in rows:
        name = f"{pipeline.short_title(show)} - {i['index'] + shift:03d} - {i['title']}{i['ext'] or '.<ext>'}"
        item = {"index": len(items) + 1, "playlist_index": i["index"], "id": i["key"],
                "path": _rel(str(show_dir / name)), "media": f"{i['kind']} · {i['source']}"}
        if shift:
            item["renamed_from"] = f"{i['index']:03d}"
        items.append(item)

    realtime = any(i["kind"] == "mse" for i in rows)
    note = (f"Page audio capture: {len(res['items'])} source(s) on “{res['title']}”. "
            "Saved without re-encoding, into the same flat folder as any audio preset.")
    if realtime:
        note += (" This player streams through Media Source, so capture happens in real time "
                 "and takes as long as the track.")
    if res["drm"]:
        note += " The page also requested DRM, which may apply to other media on it."
    if len(rows) < len(res["items"]) and not include:
        note += f" {len(res['items']) - len(rows)} already saved earlier, so skipped."
    if shift:
        note += (f" This folder already has chapters up to {existing_max:03d}, so this batch "
                 f"continues from {shift + 1:03d}.")
    return {
        "mode": p.get("naming"), "total": len(items), "shown": len(items), "truncated": False,
        "items": items, "quality": "", "note": note, "show": show,
        "author": p.get("author", ""),
        "suggestions": {
            "show": _show_suggestions({"playlist_title": res["title"]}, p.get("destination", ""),
                                      p.get("naming", "")),
            "preset": None, "metadata": {},
        },
    }


def _ext(p: dict) -> str:
    """The extension to SHOW in a preview name. Audio-only (a preset, or the
    plain checkbox on a video naming mode) never forces a re-encode, so the
    real container varies by source and can't be predicted -- shown as a
    placeholder, same as the dedicated audio-preset preview."""
    return "<ext>" if p.get("audio_only") else "mkv"


def _name_for(show, naming, p, info, run_rename):
    """Return (season_folder, filename, dated?) matching the real pipeline."""
    title = info.get("title") or info.get("id") or ""
    ext = _ext(p)
    if run_rename:
        # Season <concert year> / Show - S<year>E<mmdd> - title  (footage date)
        date = (_R.find_date(title) or _R.find_date(info.get("description") or "")
                or _R.date_from_meta(info))
        if date:
            y, mo, d = date
            return f"Season {y}", f"{show} - S{y:04d}E{mo:02d}{d:02d} - {_R.clean(title)}.{ext}", True
        return "Season (undated)", f"{show} - {_R.clean(title)}.{ext}", False

    d = _R.date_from_meta(info)   # upload/release date
    if naming == "date":
        season = p.get("season") or "1"
        if d:
            y, mo, dd = d
            return f"Season {season}", f"{show} - {y:04d}-{mo:02d}-{dd:02d} - {_R.clean(title)}.{ext}", True
        return f"Season {season}", f"{show} - {_R.clean(title)}.{ext}", False
    # datecode
    if d:
        y, mo, dd = d
        return f"Season {y}", f"{show} - S{y:04d}E{mo:02d}{dd:02d} - {_R.clean(title)}.{ext}", True
    return "Season (undated)", f"{show} - {_R.clean(title)}.{ext}", False


def _extract(p, ck, cap) -> list:
    if p.get("audio_only"):
        # Same probe shape as the dedicated audio-preset preview: -f
        # bestaudio/best means height/vcodec come back empty, so _media_str()
        # naturally shows only the audio codec/bitrate.
        fmt_args = ["-f", "bestaudio/best"]
    else:
        fmt_args = ["-f", "bv*+ba/b", "-S", _fmt_sort(p)]
    r = _ytdlp([*ck, "--simulate", *fmt_args,
                "--print", _FIELDS, "--playlist-end", str(cap), p["url"]], timeout=300)
    out = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _preview_meta(p, show_dir, ck, run_rename) -> dict:
    show, naming = p["show"], p.get("naming", "datecode")
    total = _flat_count(p, ck)
    infos = _extract(p, ck, PREVIEW_CAP)
    if not infos:
        return {"error": "No videos found (or metadata unavailable) for that URL."}

    items, undated = [], 0
    for i, info in enumerate(infos, start=1):
        pidx = info.get("playlist_index") or i
        season_dir, name, dated = _name_for(show, naming, p, info, run_rename)
        undated += 0 if dated else 1
        items.append({
            "index": i,
            "playlist_index": int(pidx) if str(pidx).isdigit() else i,
            "id": info.get("id", ""),
            "path": _rel(str(show_dir / season_dir / name)),
            "media": _media_str(info),
        })
    total = max(total, len(items))
    quality = items[0]["media"] if items else ""

    if run_rename:
        note = ("Season folders are the concert YEAR and episode codes are the concert "
                "MMDD, read from each video's title/description.")
    elif naming == "date":
        note = "One season folder (the number you set); filenames carry the upload date."
    else:
        note = "Season folders and episode codes use the upload/release date."
    if undated:
        note += f" {undated} couldn't be auto-dated and fall back to the upload date."
    note += (" Audio only: no forced re-encode, so the real extension varies by source."
              if p.get("audio_only") else " Extensions become .mkv after best-quality merging.")

    return {"mode": "footage" if run_rename else naming, "total": total,
            "shown": len(items), "truncated": total > len(items), "items": items,
            "quality": quality, "note": note}


def _preview_performance(p, show_dir, ck) -> dict:
    """Performance-name scheme: <Show>/<YYYY>/'YYYY.MM.DD - tour - venue - NNN'."""
    total = _flat_count(p, ck)
    infos = _extract(p, ck, PREVIEW_CAP)
    if not infos:
        return {"error": "No videos found (or metadata unavailable) for that URL."}

    tour_override = (p.get("tour") or "").strip() or None
    rows = []  # (year_or_undated, core, sortkey, info)
    for info in infos:
        title = info.get("title") or info.get("id") or ""
        desc = info.get("description") or ""
        date = _R.find_date(title) or _R.find_date(desc) or _R.date_from_meta(info)
        tour = _P.extract_tour(info, tour_override)
        loc = _P.parse_location(title)
        if not any(loc.values()):
            loc = _P.parse_location(desc)
        core = _P.build_core(date, tour, loc)
        pidx = info.get("playlist_index")
        pidx = int(pidx) if str(pidx).isdigit() else 0
        sortkey = (date or (9999, 99, 99), pidx)
        rows.append(((date[0] if date else "undated"), core, sortkey, info))

    groups = defaultdict(list)
    for year, core, sk, info in rows:
        groups[(year, core)].append((sk, core, info))

    entries = []  # (folder, name, info)
    for (year, core), members in groups.items():
        members.sort(key=lambda e: e[0])
        folder = str(year) if year != "undated" else "undated"
        for i, (sk, core_name, info) in enumerate(members, start=1):
            base = f"{core_name} - {i:03d}" if core_name else f"{i:03d}"
            entries.append((folder, base + "." + _ext(p), info))

    entries.sort(key=lambda e: (e[0], e[1]))
    items = []
    for folder, name, info in entries:
        pidx = info.get("playlist_index") or (len(items) + 1)
        items.append({
            "index": len(items) + 1,
            "playlist_index": int(pidx) if str(pidx).isdigit() else len(items) + 1,
            "id": info.get("id", ""),
            "path": _rel(str(show_dir / folder / name)),
            "media": _media_str(info),
        })

    quality = items[0]["media"] if items else ""
    total = max(total, len(items))
    note = ("Files land in bare year folders (no “Season”) as "
            "“YYYY.MM.DD - tour - venue, city, state, country - NNN”; missing fields "
            "(and their separators) drop out, and same-named videos get a 3-digit "
            "sequence. Venue/city usually aren’t in YouTube’s data, so many names "
            "collapse to the date (and tour). Sequence numbers span the whole "
            "playlist — this shows the first %d." % len(infos))
    return {"mode": "performance", "total": total, "shown": len(items),
            "truncated": total > len(items), "items": items, "quality": quality, "note": note}


def _preview_sequence(p, show_dir, ck) -> dict:
    """Sequential S<year>E<nn> ordered by concert date -- collision-free."""
    show = p["show"]
    total = _flat_count(p, ck)
    infos = _extract(p, ck, PREVIEW_CAP)
    if not infos:
        return {"error": "No videos found (or metadata unavailable) for that URL."}

    dated, undated = [], []
    for info in infos:
        title = info.get("title") or info.get("id") or ""
        date = (_R.find_date(title) or _R.find_date(info.get("description") or "")
                or _R.date_from_meta(info))
        (dated if date else undated).append((date, info, title))

    by_year = defaultdict(list)
    for date, info, title in dated:
        by_year[date[0]].append((date, info, title))

    items = []

    def _add(info, season_dir, name):
        pidx = info.get("playlist_index") or (len(items) + 1)
        items.append({
            "index": len(items) + 1,
            "playlist_index": int(pidx) if str(pidx).isdigit() else len(items) + 1,
            "id": info.get("id", ""),
            "path": _rel(str(show_dir / season_dir / name)),
            "media": _media_str(info),
        })

    ext = _ext(p)
    for year in sorted(by_year):
        entries = sorted(by_year[year], key=lambda e: (e[0], e[2] or ""))
        pad = max(2, len(str(len(entries))))
        for i, (date, info, title) in enumerate(entries, start=1):
            _add(info, f"Season {year}", f"{show} - S{year:04d}E{i:0{pad}d} - {_R.clean(title)}.{ext}")
    for date, info, title in undated:
        _add(info, "Season (undated)", f"{show} - {_R.clean(title)}.{ext}")

    quality = items[0]["media"] if items else ""
    total = max(total, len(items))
    note = ("Episodes are numbered sequentially within each concert year, ordered by date "
            "(unique — no collisions); the real date goes in the NFO. Final numbers span the "
            "whole playlist — this shows the first %d." % len(infos))
    if undated:
        note += f" {len(undated)} couldn't be auto-dated."
    note += (" Audio only: no forced re-encode, so the real extension varies by source."
              if p.get("audio_only") else " Extensions become .mkv after best-quality merging.")
    return {"mode": "sequence", "total": total, "shown": len(items),
            "truncated": total > len(items), "items": items, "quality": quality, "note": note}


def _preview_flat_audio(p, show_dir, ck) -> dict:
    """Any audio preset (audiobook/podcast/music): flat playlist, numbered by
    TRUE playlist position (fast, matches pipeline.flat_audio_template --
    selecting a subset never renumbers the rest). Extension is unknown ahead
    of extraction (no forced re-encode), shown as a placeholder."""
    show = p["show"]
    naming = p.get("naming", "audiobook")
    r = _ytdlp([*ck, "--flat-playlist", "--print",
                f"%(playlist_index)s{_SEP}%(id)s{_SEP}%(title)s", p["url"]], timeout=120)
    rows = [ln.split(_SEP, 2) for ln in r.stdout.splitlines() if ln.strip()]
    if not rows:
        return {"error": (r.stderr.strip().splitlines() or ["No videos found for that URL."])[-1]}
    include = set(p.get("include") or [])

    parsed = []
    for row in rows:
        pidx = row[0].strip()
        n = int(pidx) if pidx.isdigit() else len(rows)
        if include and n not in include:
            continue
        vid = row[1] if len(row) > 1 else ""
        title = (row[2] if len(row) > 2 else vid)[:pipeline.CHAPTER_TITLE_MAX]
        parsed.append((n, vid, title))

    # Mirror pipeline.continue_sequence()'s own collision rule so the preview
    # shows the NNN that will actually land on disk, not the pre-shift one --
    # same "never silently diverge from what the real run will do" rule as
    # every other preview number in this app. Existing_max==0 (nothing there
    # yet, or a destination the folder doesn't even support yet) means no
    # shift either way.
    existing_max = pipeline.existing_max_seq(show_dir) if naming in presets.AUDIO_NAMES else 0
    shift = existing_max if (parsed and min(n for n, _, _ in parsed) <= existing_max) else 0

    items = []
    for n, vid, title in parsed:
        shown_n = n + shift
        name = f"{pipeline.short_title(show)} - {shown_n:03d} - {_R.clean(title)}.<ext>"
        item = {
            "index": len(items) + 1, "playlist_index": n, "id": vid,
            "path": _rel(str(show_dir / name)),
        }
        # Per-item, not just the aggregate note below -- "would this file be
        # 001 or 133" is exactly the thing a list of otherwise-identical-
        # looking chapter files makes easy to miss if it's only spelled out
        # once in a paragraph above the list.
        if shift:
            item["renamed_from"] = f"{n:03d}"
        items.append(item)
    quality = ""
    try:
        # -f bestaudio/best (not the video _extract() probe): height/vcodec come
        # back empty for an audio-only stream, so _media_str() naturally shows
        # only the audio codec/bitrate.
        r2 = _ytdlp([*ck, "--simulate", "-f", "bestaudio/best", "--print", _FIELDS,
                     "--playlist-items", "1", p["url"]], timeout=90)
        line = next((ln for ln in r2.stdout.splitlines() if ln.strip()), "")
        quality = _media_str(json.loads(line)) if line else ""
    except (subprocess.TimeoutExpired, ValueError):
        pass
    note = (f"{presets.label(naming)}: audio-only extraction (no forced re-encode) into a "
            "flat folder, one file per playlist item, numbered by TRUE playlist position. "
            "Rename/NFO/artwork post-processing is skipped -- files land straight in the "
            "destination folder. Long titles are shortened in the FILENAME only (the show "
            f"name to initials over {pipeline.SHOW_ACRONYM_THRESHOLD} chars, the track title "
            f"cut at {pipeline.CHAPTER_TITLE_MAX}) -- the full names stay in the folder and "
            "embedded tags.")
    if shift:
        note += (f" This folder already has chapters up to {existing_max:03d}, so this batch "
                 f"will continue the sequence from {shift + 1:03d} instead of restarting at 001.")
    return {
        "mode": naming, "total": len(items), "shown": len(items), "truncated": False,
        "items": items, "quality": quality, "note": note,
    }


def _preview_episode(p, show_dir, ck) -> dict:
    """episode mode, no rename: fixed S<season>E<nnn> in playlist order (fast)."""
    show = p["show"]
    season = str(p.get("season") or "1")
    r = _ytdlp([*ck, "--flat-playlist", "--print",
                f"%(playlist_index)s{_SEP}%(id)s{_SEP}%(title)s", p["url"]], timeout=120)
    rows = [ln.split(_SEP, 2) for ln in r.stdout.splitlines() if ln.strip()]
    if not rows:
        return {"error": (r.stderr.strip().splitlines() or ["No videos found for that URL."])[-1]}
    ordered = list(reversed(rows)) if p.get("reverse", True) else rows

    items = []
    for n, row in enumerate(ordered, start=1):
        pidx = row[0].strip()
        vid = row[1] if len(row) > 1 else ""
        title = row[2] if len(row) > 2 else vid
        name = f"{show} - S{season}E{n:03d} - {_R.clean(title)}.{_ext(p)}"
        items.append({
            "index": n,
            "playlist_index": int(pidx) if pidx.isdigit() else n,
            "id": vid,
            "path": _rel(str(show_dir / f"Season {season}" / name)),
        })
    quality = ""
    try:
        probe = _extract(p, ck, 1)
        quality = _media_str(probe[0]) if probe else ""
    except subprocess.TimeoutExpired:
        pass
    return {
        "mode": "episode", "total": len(items), "shown": len(items), "truncated": False,
        "items": items, "quality": quality,
        "note": "Fixed episode numbers in playlist order. Turn on 'Seasons & episodes "
                "by performance date' to instead folder by concert year.",
    }
