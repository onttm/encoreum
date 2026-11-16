#!/usr/bin/env bash
#
# ============================================================================
#  Band A X72 -> Plex TV Series downloader
# ============================================================================
#
#  Downloads a YouTube (or Vimeo) playlist in best quality and names every
#  video so Plex sees it as a TV series, e.g.:
#
#      Band A / Season 72 / Band A - S72E001 - <video title>.mkv
#
#  WHAT YOU EDIT: only the CONFIG block below. Nothing else.
#
#  REQUIREMENTS (install once):
#      sudo apt install yt-dlp ffmpeg      # Debian/Ubuntu/Mint
#      # If your apt yt-dlp is old, prefer:  python3 -m pip install -U yt-dlp
#      # ffmpeg is REQUIRED to merge best video + best audio into one file.
#
#  HOW TO RUN:
#      chmod +x example-tour-download.sh      # make it executable (do once)
#      ./example-tour-download.sh
#
#  It is SAFE to stop it (Ctrl-C) and run it again later - already-finished
#  episodes are skipped, and the numbering stays stable.
#
#  AFTER DOWNLOADING you can run (optional):
#      python3 rename-by-footage-date.py "<library>/Band A" --show Band A
#      python3 generate-nfo.py           "<library>/Band A" --show Band A
# ============================================================================


# ------------------------------- CONFIG -------------------------------------

# The playlist to download.
PLAYLIST="https://www.youtube.com/playlist?list=PLJvQXRgtxlukmDarotT2ZFhjAVYdQHSka"

# Root of your Plex "TV Shows" library that will hold concerts.
# The show + season folders are created inside this automatically.
# ---> CHANGE THIS to your real Plex library path. <---
LIBRARY="$HOME/Plex/Concerts"

# Series (show) name and season number.
SHOW="Band A"
SEASON=72                 # X72 tour -> Season 72 (used by "episode" mode)

# Naming mode: "episode"  |  "date"  |  "datecode"
#   episode  -> Band A - S72E001 - Title.mkv         (fixed oldest->newest order)
#   date     -> Band A - 2023-04-27 - Title.mkv      (Plex sorts season by date)
#   datecode -> Band A - S2023E0427 - Title.mkv      (Season = YEAR, Ep = MMDD)
#
#   datecode uses the video's upload/release date. If that is NOT the true
#   concert (footage) date, run  rename-by-footage-date.py  afterwards to fix the
#   names using the date found in each video's title/description.
NAMING="episode"

# Only used when NAMING="episode".
# true  = the OLDEST video in the playlist becomes E001 (what you asked for).
# false = keep the playlist's own top-to-bottom order.
# (First run the preview command in the guide to confirm your playlist's order.)
REVERSE=true

# Quality preference:
#   max        = highest resolution available (may be VP9/AV1 4K; a few Plex
#                clients have to transcode those codecs)
#   compatible = prefer H.264 + AAC up to 1080p for smooth Plex "Direct Play"
QUALITY="max"

# Download English subtitles/closed-captions when present (as .srt sidecars).
# Auto-generated captions on live concerts are usually poor, so this is OFF.
WRITE_SUBS=false

# For age-restricted or members-only videos, let yt-dlp use your browser login.
# Set to your browser (firefox, chrome, chromium, edge, brave) or leave "".
COOKIES_BROWSER=""

# ----------------------------------------------------------------------------
# You normally do not need to edit anything below this line.
# ----------------------------------------------------------------------------

set -uo pipefail   # (no -e: one bad video should not kill the whole run)

DEST="$LIBRARY/$SHOW/Season $SEASON"
ARCHIVE="$LIBRARY/$SHOW-download-archive.txt"   # remembers what's done (for resume)
mkdir -p "$LIBRARY/$SHOW"

# Choose how yt-dlp ranks the available streams.
if [ "$QUALITY" = "compatible" ]; then
  FMT_SORT="res:1080,vcodec:h264,acodec:aac,fps"      # smoothest Plex playback
else
  FMT_SORT="res,fps,hdr:12,vcodec,channels,acodec"    # absolute best quality
fi

# Shared yt-dlp options (best quality + full local metadata + thumbnails).
COMMON_OPTS=(
  -f "bv*+ba/b"                 # best video + best audio, else best single file
  -S "$FMT_SORT"                # ranking rule chosen just above
  --merge-output-format mkv     # mkv is the most reliable container for Plex
  --embed-metadata              # write title/description/date INTO the video file
  --embed-chapters              # keep the concert's song chapters if present
  --embed-thumbnail             # store the thumbnail inside the file too
  --write-thumbnail             # AND save it beside the file (Plex episode image)
  --convert-thumbnails jpg      # Plex reads jpg, not YouTube's default .webp
  --write-info-json             # save .info.json (feeds the renamer + NFO builder)
  --download-archive "$ARCHIVE" # skip anything already downloaded (resume-safe)
  --ignore-errors               # skip private/removed videos instead of stopping
  --no-overwrites
  --concurrent-fragments 4      # a little faster
  --sleep-requests 1.5          # be polite -> avoids "429 Too Many Requests"
  --sleep-interval 5
  --max-sleep-interval 15
)

# Optional extras, added only if you turned them on in CONFIG.
[ "$WRITE_SUBS" = true ] && COMMON_OPTS+=(
  --write-subs --write-auto-subs --sub-langs "en.*" --convert-subs srt --embed-subs
)
[ -n "$COOKIES_BROWSER" ] && COMMON_OPTS+=( --cookies-from-browser "$COOKIES_BROWSER" )

echo "=============================================="
echo " Show    : $SHOW"
echo " Mode    : $NAMING"
echo " Save to : $LIBRARY/$SHOW"
echo "=============================================="
echo

command -v yt-dlp >/dev/null || { echo "ERROR: yt-dlp is not installed."; exit 1; }
command -v ffmpeg >/dev/null || echo "WARNING: ffmpeg not found - best-quality merging will fail."


if [ "$NAMING" = "date" ]; then
    # --------- DATE MODE: one pass, Plex orders the season by air date ---------
    # Uses each video's upload date as YYYY-MM-DD (Plex reads it from the name).
    yt-dlp "${COMMON_OPTS[@]}" \
        -o "$DEST/$SHOW - %(upload_date>%Y-%m-%d)s - %(title)s.%(ext)s" \
        "$PLAYLIST"

elif [ "$NAMING" = "datecode" ]; then
    # --------- DATECODE MODE: S<year>E<mmdd>, one season per year --------------
    # Uses the video's release date if present, else its upload date.
    yt-dlp "${COMMON_OPTS[@]}" \
        -o "$LIBRARY/$SHOW/Season %(release_date,upload_date>%Y)s/$SHOW - S%(release_date,upload_date>%Y)sE%(release_date,upload_date>%m%d)s - %(title)s.%(ext)s" \
        "$PLAYLIST"
    echo
    echo "NOTE: these names use the UPLOAD/RELEASE date. If that is not the true"
    echo "concert date, fix them from the title/description with:"
    echo "    python3 rename-by-footage-date.py '$LIBRARY/$SHOW' --show '$SHOW'"
    echo "(add --apply once the preview looks right)"

elif [ "$NAMING" = "episode" ]; then
    # --------- EPISODE MODE: fixed S72Exxx numbers in playlist order -----------
    mkdir -p "$DEST"
    echo "Reading playlist order..."
    mapfile -t IDS < <(yt-dlp --flat-playlist --print id "$PLAYLIST")
    total=${#IDS[@]}
    echo "Found $total videos."
    [ "$total" -eq 0 ] && { echo "ERROR: got 0 videos - check the URL/network."; exit 1; }

    # Build the final order (oldest first if REVERSE=true).
    ORDERED=()
    if [ "$REVERSE" = true ]; then
        for (( i=total-1; i>=0; i-- )); do ORDERED+=("${IDS[i]}"); done
    else
        ORDERED=("${IDS[@]}")
    fi

    # Download each one with its fixed episode number.
    n=1
    for id in "${ORDERED[@]}"; do
        ep=$(printf "E%03d" "$n")
        echo
        echo ">>> S${SEASON}${ep}  (video $n of $total)"
        yt-dlp "${COMMON_OPTS[@]}" \
            -o "$DEST/$SHOW - S${SEASON}${ep} - %(title)s.%(ext)s" \
            "https://www.youtube.com/watch?v=$id" \
            || echo "!!! Skipped S${SEASON}${ep} (video unavailable) - number left as a gap."
        n=$((n+1))
    done

else
    echo "ERROR: NAMING must be 'episode', 'date', or 'datecode'."; exit 1
fi

echo
echo "Download complete. Recommended next steps:"
echo "  1) (datecode from true concert date)  python3 rename-by-footage-date.py '$LIBRARY/$SHOW' --show '$SHOW'"
echo "  2) (rich Plex metadata)               python3 generate-nfo.py '$LIBRARY/$SHOW' --show '$SHOW'"
echo "  3) Add/scan the library in Plex (see the guide)."
