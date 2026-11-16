# Docker secrets — notes for this project

Written 2026-08-21 after getting it wrong twice. Sources: the GitGuardian
secrets-in-Docker guide, Snyk's Docker image security practices, the Wiz
container-security guide, and the OWASP Docker Security Cheat Sheet — plus
things measured on **this** host, which is where the surprises were.

---

## The rule

**File mounts, never environment variables.** Every credential is a Docker
secret read from `/run/secrets/`. None appears in `environment:`.

Why it matters concretely, not abstractly — an env var credential is:

- **printed in full by `docker inspect`**, so anyone in the `docker` group has
  it. They are already root-equivalent on the host, but this hands them keys to
  *other* services;
- **inherited by every child process** — this app spawns `yt-dlp`, `streamlink`
  and `ffmpeg`, so a Plex token in the environment is in all of theirs;
- present in `/proc/<pid>/environ`, crash dumps, and any config-dumping log.

A file at `/run/secrets/` has none of those properties.

## What I got wrong

**1. Applied it to one credential out of three.** setlist.fm was moved to a
secret and documented, while `PLEX_TOKEN` and `LIDARR_API_KEY` stayed plain env
vars — including the token that had already leaked. Half-applied discipline is
worse than none: the docs claimed a property the system did not have.

**2. Weakened the file permissions to make it work.** First attempt owned the
host file `1000:1000 0600` so the app could read it. That leans on the secrets
*directory* being untraversable rather than on the file itself — it fails open
the day those directory permissions change, and it puts the key under the
operator's own account. Correct is `root:<dedicated-group> 0640` with only the
one container granted the group.

## Three things measured here that the tutorials do not cover

**`uid`/`gid`/`mode` on a secret are ignored by Compose.** Not a rumour —
Compose says so:

```
warning: secrets `uid`, `gid` and `mode` are not supported, they will be ignored
```

Tested: requested `uid: "1000", gid: "1000", mode: 0400` against a `root:root
0600` host file; it mounted `root:root 0600` and a uid-1000 process could not
read it. **Permissions must be managed on the HOST file.** (Swarm honours these;
plain Compose does not, even on a node where Swarm is active.)

**`group_add` does not survive `gosu`.** The nastiest one. Compose's `group_add`
adds supplementary groups to the container's *init* process, but `gosu`
re-initialises the group list from `/etc/group` when it drops privileges, so the
injected gid is silently discarded. The symptom is maddening: `docker exec -u
1000` reads the secret fine while the app reports permission denied.

`entrypoint.sh` handles it generically — for each file in `/run/secrets`, take
the gid the host file carries, ensure a group with that gid exists in the
container, add `appusr` to it, *then* drop privileges.

**`docker exec` runs as root by default.** Every `exec` that writes a file
leaves it root-owned in a volume the app can only touch as uid 1000, surfacing
much later as a `PermissionError` far from its cause. Use `-u 1000:1000`.

## The setup

Host, one dedicated group, granted to exactly one container:

```
/home/you/docker/secrets/
  encoreum_plex_token          root:encoreum-secret 0640
  encoreum_lidarr_api_key      root:encoreum-secret 0640
  encoreum_setlist_fm_api_key  root:encoreum-secret 0640
```

`your-user` is **not** in `encoreum-secret`, so the keys stay unreadable by the
operator account regardless of directory permissions.

Compose declares them and passes only *paths*:

```yaml
environment:
  - PLEX_TOKEN_FILE=/run/secrets/plex_token
  - LIDARR_API_KEY_FILE=/run/secrets/lidarr_api_key
  - SETLISTFM_API_KEY_FILE=/run/secrets/setlistfm_api_key
secrets: [plex_token, lidarr_api_key, setlistfm_api_key]
group_add: ["985"]        # encoreum-secret
```

`app/secrets.py` reads `$NAME_FILE` → `/run/secrets/<name>` → `$NAME`, the same
convention postgres/mysql/nextcloud images use. The env fallback stays so the
app runs for someone without secrets configured — a self-hosted tool that
refuses to start is worse for whoever inherits it — but `source_of()` reports
which was used, and `GET /api/credentials` marks env-var sourcing as
`secure: false`. It never returns a value.

Values are `.strip()`ed: a trailing newline from an editor otherwise produces a
flat 401/403 with nothing pointing at whitespace.

## Verification

```bash
docker inspect encoreum --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E 'TOKEN|API_KEY'
# must show only *_FILE paths, never a value
curl -s localhost:8681/api/credentials     # every entry secure: true
docker logs encoreum | grep secret       # appusr joined each secret's group
```

## Not done yet

- **`USER 1000` in the Dockerfile** (Snyk). This image starts as root and drops
  via `gosu` so it can remap PUID/PGID and join secret groups at runtime. That
  is a real trade-off, not an oversight — but the root phase should be audited.
- **`read_only: true` with explicit `tmpfs`** for writable paths (OWASP/Wiz).
- **`cap_drop: [ALL]`** and a seccomp profile.
- **Rotation.** Nothing rotates these. The `_TTL` cache means a rotated secret
  takes effect within 60s without a restart, but rotation itself is manual.
- **BuildKit `--mount=type=secret`** is not needed here — no build-time creds.
