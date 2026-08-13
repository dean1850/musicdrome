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

# DENO_DIR lands in /config because that is the one directory guaranteed to be
# writable whoever we end up running as — the entrypoint chowns it to PUID/PGID
# before dropping. Deno otherwise wants $HOME/.cache/deno, and yt-dlp treats
# *any* output on deno's stderr as a failed challenge, so a cache directory it
# cannot create is not a warning: it is every download failing.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MUSICDROME_HOST=0.0.0.0 \
    MUSICDROME_PORT=3046 \
    MUSICDROME_MUSIC_DIR=/music \
    MUSICDROME_DATA_DIR=/config \
    DENO_DIR=/config/.deno

# ffmpeg does the 320 kbps encode; tini reaps the yt-dlp and ffmpeg children;
# gosu drops to PUID/PGID when those are not root.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        tini \
        gosu \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# A JavaScript runtime is what lets YouTube downloads work at all: the clients
# that still serve plain HTTPS format URLs need one to solve YouTube's
# signature and n-challenges, and without it yt-dlp falls back to clients that
# either carry no format URLs (yt-dlp/yt-dlp#12482) or answer with HTTP 403.
#
# This was `apt-get install nodejs` and it silently did nothing for us: yt-dlp
# requires Node >= 22.0.0 and Debian ships 18 (bookworm) / 20 (trixie), so the
# runtime was detected, judged unsupported and ignored. Deno is yt-dlp's own
# default and recommended runtime, and it is not in Debian at any version — so
# it comes from Deno's binary-only image, which is multi-arch and verifies its
# own checksum at publish time. Keep this at or above yt-dlp's floor of 2.3.0.
COPY --from=denoland/deno:bin-2.9.5 /deno /usr/local/bin/deno

# Catch a wrong tag or a moved path here rather than in a 403 six hours into a
# scan. The version probe is deliberately soft: the arm64 image is cross-built
# under QEMU, where running the emulated binary proves little either way. The
# hard assertion is that an executable deno exists, and app.download's boot
# check has the last word on whether yt-dlp will actually use it.
RUN set -eux; \
    test -x /usr/local/bin/deno; \
    deno --version || echo "WARNING: deno did not run at build time (expected when cross-building)"

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
