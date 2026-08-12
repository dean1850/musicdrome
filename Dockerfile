# ═══════════════════════════════════════════════════════════════════════════
#  Musicdrome — production image.
#
#  One stage. The UI is plain HTML, CSS and JavaScript, so there is nothing to
#  compile and no Node in the build at all.
# ═══════════════════════════════════════════════════════════════════════════

FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/dean1850/musicdrome" \
      org.opencontainers.image.title="Musicdrome" \
      org.opencontainers.image.description="AI music discovery that downloads what it recommends"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MUSICDROME_HOST=0.0.0.0 \
    MUSICDROME_PORT=3046 \
    MUSICDROME_MUSIC_DIR=/music \
    MUSICDROME_DATA_DIR=/config

# ffmpeg does the 320 kbps encode; tini reaps the yt-dlp and ffmpeg children;
# gosu drops to PUID/PGID when those are not root.
#
# nodejs is what lets YouTube downloads work at all. The clients that still
# serve plain HTTPS format URLs need a JavaScript runtime to solve YouTube's
# signature and n-challenges; without one, yt-dlp is left with SABR-only
# clients and reports "formats have been skipped as they are missing a URL"
# (yt-dlp/yt-dlp#12482). yt-dlp prefers deno, but deno is not packaged for
# Debian and Node is, so Node it is.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        tini \
        gosu \
        curl \
        ca-certificates \
        nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/app/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh && mkdir -p /music /config

VOLUME ["/config"]
EXPOSE 3046

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${MUSICDROME_PORT}/api/health" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
