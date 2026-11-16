FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# deno = the JavaScript runtime yt-dlp uses to fully extract modern YouTube
# formats. Without it yt-dlp warns and "some formats may be missing" -- which
# matters when we're chasing best-quality concert streams.
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

# ffmpeg  -> yt-dlp needs it to merge best video + best audio
# aria2   -> optional faster fragment downloader
# gosu    -> drop from root to PUID/PGID after the entrypoint sets ownership
# tzdata / ca-certificates -> correct time + TLS
# passwd  -> useradd/groupadd/usermod for the PUID/PGID juggling
# chromium -> page audio capture (scripts/capture-page-audio.py), driven by the
#             Playwright package in requirements.txt. Debian's build rather than
#             Playwright's own download, so `apt` keeps it patched and it plays
#             AAC/MP3, which Playwright's bundled Chromium does not fully do.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg aria2 gosu tzdata ca-certificates passwd chromium \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/     /app/app/
COPY scripts/ /app/scripts/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh /app/scripts/*.py /app/scripts/*.sh

EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
