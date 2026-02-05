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
