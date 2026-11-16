# Percia Bridge: multi-arch (linux/amd64, linux/arm64).
#
# ffmpeg: Debian's packages (7.1.x in trixie) are built WITHOUT the `whip` muxer, so we ship a pinned static build
# (BtbN/FFmpeg-Builds, GPL, includes OpenSSL for DTLS and libx264). It is verified by SHA-256 per architecture, and the
# build fails if the result has no `whip` muxer or is older than 7.1.
# To bump: pick a release at https://github.com/BtbN/FFmpeg-Builds/releases, update FFMPEG_TAG/FFMPEG_NAME and the two sums
# from that release's checksums.sha256. (BtbN prunes old autobuild tags: if a build fails with 404, bump the pin.)
ARG FFMPEG_TAG=autobuild-2026-11-08-12-35
ARG FFMPEG_NAME=ffmpeg-n8.1.3-34-ga35157abb9
ARG FFMPEG_SHA256_AMD64=4af8ddf89ba988554a8373dfdae378eaafb774d5289b38e584ff2bd3d3a2b0ec
ARG FFMPEG_SHA256_ARM64=5b9ad4467185f69b3c1f2dd3a17ef5cca41c2b25f38ca527c93c6b8bc3789c07

FROM debian:trixie-slim AS ffmpeg
ARG TARGETARCH
ARG FFMPEG_TAG
ARG FFMPEG_NAME
ARG FFMPEG_SHA256_AMD64
ARG FFMPEG_SHA256_ARM64
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl xz-utils && rm -rf /var/lib/apt/lists/*
RUN set -eu; \
    case "$TARGETARCH" in amd64) a=linux64; sum="$FFMPEG_SHA256_AMD64";; arm64) a=linuxarm64; sum="$FFMPEG_SHA256_ARM64";; *) echo "unsupported arch $TARGETARCH"; exit 1;; esac; \
    f="${FFMPEG_NAME}-${a}-gpl-8.1.tar.xz"; \
    curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 -o /tmp/ff.txz "https://github.com/BtbN/FFmpeg-Builds/releases/download/${FFMPEG_TAG}/${f}"; \
    echo "${sum}  /tmp/ff.txz" | sha256sum -c -; \
    mkdir /ff && tar -xJf /tmp/ff.txz -C /ff --strip-components=2 --wildcards '*/bin/ffmpeg' '*/bin/ffprobe'
# Refuse to build an image that cannot publish over WHIP.
RUN /ff/ffmpeg -hide_banner -muxers | grep -qE '^ +E +whip ' && /ff/ffmpeg -hide_banner -encoders | grep -q ' libx264 ' \
 && v=$(/ff/ffmpeg -version | sed -n '1s/.*version n\?\([0-9]*\)\.\([0-9]*\).*/\1 \2/p') \
 && set -- $v && { [ "$1" -gt 7 ] || { [ "$1" -eq 7 ] && [ "$2" -ge 1 ]; }; } || { echo "ffmpeg >= 7.1 with whip + libx264 required"; exit 1; }

FROM python:3.15-slim-trixie
RUN apt-get update && apt-get install -y --no-install-recommends tini ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=ffmpeg /ff/ffmpeg /ff/ffprobe /usr/local/bin/

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . \
 && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin bridge \
 && mkdir -p /config /data && chown 10001:10001 /config /data \
 && ffmpeg -hide_banner -muxers | grep -qE '^ +E +whip '

ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="percia-bridge" \
      org.opencontainers.image.description="Sends cameras on your own network (ONVIF, RTSP, Frigate, Home Assistant) to Percia over WHIP" \
      org.opencontainers.image.source="https://github.com/velvee-ai/percia-bridge" \
      org.opencontainers.image.licenses="MIT (percia-bridge); the bundled ffmpeg is GPL-3.0, see https://github.com/BtbN/FFmpeg-Builds" \
      org.opencontainers.image.revision="${GIT_SHA}"

ENV PERCIA_BRIDGE_VARIANT=docker PYTHONUNBUFFERED=1
USER 10001
VOLUME ["/config", "/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 CMD ["percia-bridge", "status", "--quiet"]
ENTRYPOINT ["tini", "--", "percia-bridge"]
CMD ["run"]
