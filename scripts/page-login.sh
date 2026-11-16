#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# page-login.sh -- log in to a site once, so page capture can use that session.
#
#   ./scripts/page-login.sh https://example.com/account
#
# Starts a TEMPORARY browser you can reach from your own browser, waits while
# you log in (PIN, 2FA and all), saves the session to
# config/sessions/<host>.json, then deletes the temporary browser. Encoreum uses
# that file automatically for pages on the same host.
#
# You type your credentials into the site itself; this script never sees them.
# The saved file holds live login tokens: it is mode 600, gitignored, and
# included in anything that backs up config/. Delete it to revoke local use, and
# sign the device out on the site to revoke the token itself.
#
# Options (environment):
#   PORT        host port for the temporary browser (default 3004)
#   BIND        address to publish on (default: all interfaces). The temporary
#               browser has NO password, so it's open to your LAN while it runs.
# ---------------------------------------------------------------------------
set -euo pipefail

URL=${1:-}
if [ -z "$URL" ]; then
    echo "usage: $0 <page-url>" >&2
    exit 2
fi
case "$URL" in
    http://*|https://*) ;;
    *) echo "ERROR: the URL must start with http:// or https://" >&2; exit 2 ;;
esac

REPO=$(cd "$(dirname "$0")/.." && pwd)
HOST=$(python3 -c 'import sys,urllib.parse as u; print((u.urlsplit(sys.argv[1]).hostname or "").lower())' "$URL")
PORT=${PORT:-3004}
BIND=${BIND:-0.0.0.0}
NAME=encoreum-login
BROWSER_IMAGE=lscr.io/linuxserver/chromium:latest
APP_IMAGE=${APP_IMAGE:-encoreum:local}
OUT="$REPO/config/sessions"

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

docker run -d --name "$NAME" --shm-size=1g \
    -p "$BIND:$PORT:3000" \
    -e PUID="$(id -u)" -e PGID="$(id -g)" \
    -e "CHROME_CLI=--remote-debugging-port=9222 --user-data-dir=/config/encoreum-login $URL" \
    "$BROWSER_IMAGE" >/dev/null

LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "  1. Open http://${LAN_IP:-localhost}:$PORT in your browser."
echo "  2. Log in to the site there, and open a page that plays audio."
echo "  3. Come back here and press Enter. (Ctrl-C cancels and saves nothing.)"
echo
echo "  This temporary browser has no password while it runs."
read -r _

mkdir -p "$OUT"
chmod 700 "$OUT"
docker run --rm --network "container:$NAME" \
    --user "$(id -u):$(id -g)" \
    -v "$OUT:/sessions" \
    --entrypoint python3 "$APP_IMAGE" \
    /app/scripts/capture-page-audio.py "$URL" \
    --cdp http://127.0.0.1:9222 --save-session "/sessions/$HOST.json"
echo "Done. Pages on $HOST will now use this login."
