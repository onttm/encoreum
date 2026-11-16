"""
presets.py -- single source of truth for what a `naming` value means.

Video presets ARE their layout scheme (performance/sequence/datecode/episode/
date) -- the name IS the behavior, so they just carry a label here for the
dropdown. Audio presets are a different axis entirely: content TYPE, not
layout. They all share one mechanical download path (flat folder, audio-only,
numbered by true playlist position -- see pipeline.flat_audio_template) and
differ only in where they land and what they're for. Adding a fourth audio
preset should mean adding one dict entry here, not touching four files.
"""

VIDEO = "video"
AUDIO = "audio"

PRESETS = {
    "performance": {
        "category": VIDEO,
        "label": "performance — YYYY.MM.DD - tour - venue - NNN (year folders, no “Season”)",
    },
    "sequence": {
        "category": VIDEO,
        "label": "sequence — S<year>E<nn> by concert date (no collisions)",
    },
    "datecode": {
        "category": VIDEO,
        "label": "datecode — S<year>E<mmdd> (collides if same date)",
    },
    "episode": {
        "category": VIDEO,
        "label": "episode — S<season>E<nnn>",
    },
    "date": {
        "category": VIDEO,
        "label": "date — Show - YYYY-MM-DD",
    },
    "audiobook": {
        "category": AUDIO,
        "label": "Audiobook",
        "addon": "Audiobooks for Audiobookshelf: Author/Title folders, one file per chapter.",
        "default_dest": "audiobooks",
        # Audiobookshelf's mounted library root -- see docker-compose.yml.
    },
    "podcast": {
        "category": AUDIO,
        "label": "Podcast",
        "addon": "Podcast episodes as audio, one flat folder per show.",
        "default_dest": "podcasts",
        # $PODCASTSDIR, nested inside the /library mount -- see docker-compose.yml.
    },
    "music": {
        "category": AUDIO,
        "label": "Music library (live audio)",
        "default_dest": "live-music",
        # Same folder app/imports.py's approved taper uploads already land in
        # (LIVE_AUDIO_DIR) -- this preset and manual ingest share one library.
    },
}

NAMES = tuple(PRESETS)
# ADD-ONS (2026-11-16): the core is live music -- concerts, streams, setlists,
# taper uploads and the live-audio preset. Audiobook and Podcast are extras on
# the same download path, OFF until switched on in the UI (settings.json
# "addons"). Off hides them from new jobs and suggestions only: existing
# folders, history and retries of old jobs keep working.
ADDON_NAMES = tuple(k for k, v in PRESETS.items() if v.get("addon"))
AUDIO_NAMES = tuple(k for k, v in PRESETS.items() if v["category"] == AUDIO)
VIDEO_NAMES = tuple(k for k, v in PRESETS.items() if v["category"] == VIDEO)

# Extensions this app's pipeline ever produces, for the places that need to
# recognize "is this a real media file" (library.py's tree/counts, jobs.py's
# error-vs-success check). Used to be separate copies of roughly this set
# (plus a third, unused one in pipeline.py) that drifted independently --
# centralized here since presets.py is already the one place that knows what
# a preset outputs.
VIDEO_EXTS = {".mkv", ".mp4", ".webm", ".m4v", ".avi", ".mov"}
AUDIO_EXTS = {".opus", ".m4a", ".m4b", ".mp3", ".flac", ".ogg", ".oga", ".wav", ".aac"}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS


def is_audio(naming: str) -> bool:
    return PRESETS.get(naming, {}).get("category") == AUDIO


def default_dest(naming: str) -> str | None:
    return PRESETS.get(naming, {}).get("default_dest")


def is_addon(naming: str) -> bool:
    return naming in ADDON_NAMES


def addon_blurb(naming: str) -> str:
    return PRESETS.get(naming, {}).get("addon", "")


def label(naming: str) -> str:
    return PRESETS.get(naming, {}).get("label", naming)
