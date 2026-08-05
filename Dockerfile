# ═══════════════════════════════════════════════════════════════════════════
#  Musicdrome — production image
#  Stage 1 builds the React UI, stage 2 runs FastAPI and serves the built UI.
# ═══════════════════════════════════════════════════════════════════════════

# ─── Stage 1: frontend ─────────────────────────────────────────────────────
# Pinned to the builder's own platform — the output is static JS/CSS, so there
# is nothing to gain from running node under emulation on a cross build.
FROM --platform=$BUILDPLATFORM node:22-alpine AS frontend

WORKDIR /build

COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund

COPY frontend/ ./
RUN npm run build


# ─── Stage 2: runtime ──────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Links the published package back to this repository on GHCR.
LABEL org.opencontainers.image.source="https://github.com/dean1850/musicdrome" \
      org.opencontainers.image.title="Musicdrome" \
      org.opencontainers.image.description="Self-hosted music server with a Subsonic API, AI playlists and podcasts"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MUSICDROME_STATIC_DIR=/app/static

# ffmpeg powers transcoding and yt-dlp post-processing; tini reaps the
# ffmpeg/yt-dlp children we spawn.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        tini \
        curl \
        ca-certificates \
        gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ /app/
COPY --from=frontend /build/dist /app/static

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY docker/musicdrome /usr/local/bin/musicdrome
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/musicdrome \
    && mkdir -p /music /config /cache /podcasts /downloads

VOLUME ["/config", "/cache", "/podcasts", "/downloads"]
EXPOSE 4533

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${MUSICDROME_PORT:-4533}/api/v1/health" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["serve"]
