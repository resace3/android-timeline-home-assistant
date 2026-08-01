# https://developers.home-assistant.io/docs/add-ons/configuration#add-on-dockerfile
#
# There is no build.yaml: the legacy home-assistant/builder is retired, and
# the base image, build arguments and labels now live here so the image can
# be built by Docker BuildKit directly.
ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.13-alpine3.23-2026.06.1
FROM ${BUILD_FROM}

ARG TARGETARCH
ARG BUILD_ARCH
ARG BUILD_VERSION="0.0.0"

SHELL ["/bin/ash", "-o", "pipefail", "-c"]

WORKDIR /opt/android-timeline

# Runtime dependencies are pinned. The service is the only place in this
# project with third-party dependencies at all -- the phone-side collector
# is deliberately stdlib-only.
COPY requirements.txt ./
RUN \
    if [ -z "${TARGETARCH}" ]; then \
        echo "TARGETARCH is not set; build with Docker BuildKit." && exit 1; \
    fi \
    && pip install --no-cache-dir --requirement requirements.txt \
    && find /usr/lib/python3*/ -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

COPY app ./app
COPY rootfs /

# s6 service scripts must be executable regardless of the checkout's mode
# bits, which git on some platforms does not preserve.
RUN chmod a+x /etc/services.d/android-timeline/run \
    && chmod a+x /etc/services.d/android-timeline/finish

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ANDROID_TIMELINE_DATA_DIR=/data

LABEL \
    io.hass.type="app" \
    org.opencontainers.image.title="Home Assistant App: Android Timeline" \
    org.opencontainers.image.description="Ingestion, feature engineering and a read-only MCP server for Android Timeline data" \
    org.opencontainers.image.source="https://github.com/resace3/android-timeline-home-assistant" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.version="${BUILD_VERSION}"
