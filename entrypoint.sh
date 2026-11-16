#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Encoreum entrypoint
#
# Runs as root, does three things, then hands off to the app as a normal user:
#   1. Re-maps our internal "appusr" account to the PUID/PGID you set, so any
#      file written into /library is owned by the same host user Plex runs as.
#   2. Makes sure /config (job history, logs, download-archives) and the yt-dlp
#      virtual-env exist and are owned by that user.
#   3. Installs / upgrades yt-dlp into that venv so the app can update it later
#      *without* needing root (see app/updater.py).
# Finally it drops privileges with gosu and execs the CMD (uvicorn).
# ---------------------------------------------------------------------------
set -e

PUID=${PUID:-1000}
PGID=${PGID:-1000}
VENV=/config/venv

echo "[entrypoint] starting as PUID=$PUID PGID=$PGID"

# 1. Line up the appusr/appgrp ids with what the host expects.
groupadd -o -g "$PGID" appgrp 2>/dev/null || groupmod -o -g "$PGID" appgrp
id appusr >/dev/null 2>&1 || useradd -o -u "$PUID" -g appgrp -d /config -s /bin/bash appusr
usermod -o -u "$PUID" -g "$PGID" appusr

# 2. Directory scaffolding, then hand /config to the app user.
mkdir -p /config/logs /config/archives /library
chown -R "$PUID:$PGID" /config

# --- docker secrets -------------------------------------------------------
# Make every mounted secret readable by the app user, WITHOUT widening the file.
#
# The subtlety that makes this necessary: Compose `group_add` adds supplementary
# groups to the container's init process, but `gosu` re-initialises the group
# list from /etc/group when it drops privileges — so an injected gid is silently
# dropped and the app sees "permission denied" while `docker exec -u 1000` can
# read the file fine. That mismatch is very hard to diagnose from inside the app.
#
# So for each secret, take the group the HOST file is owned by (Compose
# preserves it; the uid/gid/mode keys in a secret definition are Swarm-only and
# ignored here), ensure a group with that gid exists in the container, and make
# appusr a member. Nothing on the host changes and no file is made
# world-readable.
if [ -d /run/secrets ]; then
    for s in /run/secrets/*; do
        [ -e "$s" ] || continue
        sgid=$(stat -c '%g' "$s")
        [ "$sgid" = "0" ] && continue          # root-owned group: nothing to join
        [ "$sgid" = "$PGID" ] && continue       # already the app's own group
        gname=$(getent group "$sgid" | cut -d: -f1)
        if [ -z "$gname" ]; then
            gname="secret$sgid"
            groupadd -o -g "$sgid" "$gname" 2>/dev/null || true
        fi
        usermod -aG "$gname" appusr 2>/dev/null || true
        echo "[entrypoint] secret $(basename "$s"): appusr joined group $gname ($sgid)"
    done
fi

# 3. yt-dlp lives in a venv under the (persistent, user-owned) /config volume.
#    That is what lets the non-root app self-update yt-dlp on demand.
if ! gosu appusr "$VENV/bin/python" -c "pass" 2>/dev/null; then
    echo "[entrypoint] creating yt-dlp virtual-env at $VENV"
    rm -rf "$VENV"
    gosu appusr python -m venv "$VENV"
    gosu appusr "$VENV/bin/pip" install --upgrade --quiet pip
fi

echo "[entrypoint] updating yt-dlp (pip -U) ..."
gosu appusr "$VENV/bin/pip" install --upgrade --quiet yt-dlp 2>&1 | tee /config/last-update.log || true
echo "[entrypoint] yt-dlp version: $(gosu appusr "$VENV/bin/yt-dlp" --version 2>/dev/null || echo 'unknown')"

# mutagen: yt-dlp's --embed-metadata needs it for Ogg/Opus containers (ffmpeg
# alone covers mp4/mp3/mkv tagging, but Vorbis-comment formats go through
# mutagen). The audiobook naming mode's audio-only extraction can land on
# Opus, so without this its metadata-embed postprocessor step fails.
gosu appusr "$VENV/bin/pip" install --upgrade --quiet mutagen

exec gosu appusr "$@"
