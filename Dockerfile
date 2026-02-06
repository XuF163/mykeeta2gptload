#
# Build gpt-load from source (fork-friendly).
# We do this because a fork may not have a prebuilt image / GHCR permissions.
#
# Default repo points to the user's fork; change via build args if needed.
#
ARG GPT_LOAD_REPO=https://github.com/XuF163/gpt-load.git
ARG GPT_LOAD_REF=main
ARG GPT_LOAD_VERSION=dev

FROM alpine:3.20 AS gptload-src
ARG GPT_LOAD_REPO
ARG GPT_LOAD_REF
RUN apk add --no-cache git
WORKDIR /src
# Prefer a shallow clone for branches/tags; fall back to full clone for commit refs.
RUN set -eux; \
    (git clone --depth 1 --branch "$GPT_LOAD_REF" "$GPT_LOAD_REPO" /src) || \
    (git clone "$GPT_LOAD_REPO" /src && cd /src && git checkout "$GPT_LOAD_REF")

FROM node:20-alpine AS gptload-web
ARG GPT_LOAD_VERSION
WORKDIR /build
COPY --from=gptload-src /src/web /build
RUN npm install
RUN VITE_VERSION=${GPT_LOAD_VERSION} npm run build

FROM golang:alpine AS gptload-build
ARG GPT_LOAD_VERSION
ENV GO111MODULE=on \
    CGO_ENABLED=0 \
    GOOS=linux
WORKDIR /build
COPY --from=gptload-src /src/go.mod /src/go.sum /build/
RUN go mod download
COPY --from=gptload-src /src /build
COPY --from=gptload-web /build/dist /build/web/dist
RUN go build -ldflags "-s -w -X gpt-load/internal/version.Version=${GPT_LOAD_VERSION}" -o /out/gpt-load

FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive

# System deps:
# - chromium: required by DrissionPage (CDP automation)
# - fonts: avoid missing glyphs / blank pages in some locales
# - curl/ca-certificates: install uv + TLS
ARG INSTALL_CJK_FONTS=0
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      chromium \
      tzdata \
      xvfb \
      xauth \
      fonts-liberation; \
    if [ "${INSTALL_CJK_FONTS}" = "1" ]; then \
      apt-get install -y --no-install-recommends fonts-noto-cjk; \
    fi; \
    rm -rf /var/lib/apt/lists/*

# Some tools look for google-chrome; Chromium binary is usually /usr/bin/chromium.
RUN ln -sf /usr/bin/chromium /usr/bin/google-chrome || true

# Install uv (Astral)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# Keep uv-managed venv outside the bind-mounted repo directory.
# This avoids losing the environment when we mount the project into /app.
ENV UV_PROJECT_ENVIRONMENT=/opt/uv-venv
ENV UV_CACHE_DIR=/tmp/uv-cache

WORKDIR /app

# gpt-load (internal service for HF Spaces). The binary is static (CGO=0).
COPY --from=gptload-build /out/gpt-load /usr/local/bin/gpt-load

# Install Python deps at build time for prebuilt images (GitHub Actions / GHCR).
# We copy only the dependency manifests first to maximize Docker layer caching.
COPY pyproject.toml uv.lock README.md requirements.txt /app/
RUN uv sync --frozen --no-install-project \
  && rm -rf "${UV_CACHE_DIR}" /root/.cache/uv

# Copy the rest of the project (config.toml is excluded via .dockerignore).
COPY . /app

# HuggingFace Spaces expects a long-running process listening on $PORT.
# We run a tiny HTTP server that can trigger the job on demand.
CMD ["sh", "-lc", "python hf_server.py"]
