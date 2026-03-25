# Lightweight Nexus server image for demo/shared presets.
# Installs from local source when build context contains pyproject.toml + src/
# (repo-root checkout), falls back to PyPI for standalone portable use.
# Issue #2915.

FROM python:3.13-slim

ARG NEXUS_VERSION=latest
ARG NEXUS_TXTAI_USE_API_EMBEDDINGS=false
ARG TARGETARCH

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/app/data/.cache/huggingface \
    LD_PRELOAD="/usr/lib/libgomp.so.1 /usr/lib/libc10.so" \
    NEXUS_HOST=0.0.0.0 \
    NEXUS_PROFILE=full \
    NEXUS_PORT=2026 \
    NEXUS_DATA_DIR=/app/data \
    SENTENCE_TRANSFORMERS_HOME=/app/data/.cache/sentence-transformers

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    ca-certificates \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    if [ "${TARGETARCH}" = "arm64" ]; then \
        ln -sf /usr/lib/aarch64-linux-gnu/libgomp.so.1 /usr/lib/libgomp.so.1; \
    elif [ "${TARGETARCH}" = "amd64" ]; then \
        ln -sf /usr/lib/x86_64-linux-gnu/libgomp.so.1 /usr/lib/libgomp.so.1; \
    fi

RUN pip install --no-cache-dir uv

WORKDIR /app

# Stage the full build context so we can detect local source.
# When the build context is the repo root, this includes pyproject.toml + src/.
# When the build context is a portable temp dir, only compose/Dockerfile files.
COPY . /tmp/nexus-build/

# Install CPU-only torch first to avoid ~4 GB of CUDA libraries.
RUN uv pip install --system --index-url https://download.pytorch.org/whl/cpu torch

# Install: prefer local source (repo checkout), fall back to PyPI.
RUN if [ -f /tmp/nexus-build/pyproject.toml ] && [ -d /tmp/nexus-build/src ]; then \
      echo "Installing from local source..."; \
      cd /tmp/nexus-build && if [ "$NEXUS_TXTAI_USE_API_EMBEDDINGS" = "true" ]; then \
        uv pip install --system ".[semantic-search-remote]" "txtai[ann]>=9.0" "pgvector>=0.3.0"; \
      else \
        uv pip install --system ".[semantic-search]" "txtai[ann]>=9.0"; \
      fi; \
    elif [ "$NEXUS_VERSION" = "latest" ]; then \
      echo "Installing from PyPI (latest)..."; \
      if [ "$NEXUS_TXTAI_USE_API_EMBEDDINGS" = "true" ]; then \
        uv pip install --system "nexus-ai-fs[semantic-search-remote]" "txtai[ann]>=9.0" "pgvector>=0.3.0"; \
      else \
        uv pip install --system "nexus-ai-fs[semantic-search]" "txtai[ann]>=9.0"; \
      fi; \
    else \
      echo "Installing from PyPI (${NEXUS_VERSION})..."; \
      if [ "$NEXUS_TXTAI_USE_API_EMBEDDINGS" = "true" ]; then \
        uv pip install --system "nexus-ai-fs[semantic-search-remote]==${NEXUS_VERSION}" "txtai[ann]>=9.0" "pgvector>=0.3.0"; \
      else \
        uv pip install --system "nexus-ai-fs[semantic-search]==${NEXUS_VERSION}" "txtai[ann]>=9.0"; \
      fi; \
    fi && rm -rf /tmp/nexus-build

RUN ln -sf /usr/local/lib/python3.13/site-packages/torch/lib/libc10.so /usr/lib/libc10.so

RUN useradd -r -m -u 1000 -s /bin/bash nexus \
    && mkdir -p /app/data \
    && chown -R nexus:nexus /app

USER nexus

EXPOSE 2026

HEALTHCHECK --interval=10s --timeout=5s --start-period=60s --retries=10 \
    CMD curl -f http://localhost:${NEXUS_PORT:-2026}/health || exit 1

# Start nexusd directly — no entrypoint scripts in lightweight mode.
# The CLI's demo init reads the API key from config or the key file.
CMD exec nexusd \
      --host ${NEXUS_HOST} \
      --port ${NEXUS_PORT} \
      --data-dir ${NEXUS_DATA_DIR} \
      ${NEXUS_PROFILE:+--profile $NEXUS_PROFILE} \
      ${NEXUS_DATABASE_URL:+--database-url $NEXUS_DATABASE_URL} \
      ${NEXUS_AUTH_TYPE:+--auth-type $NEXUS_AUTH_TYPE} \
      ${NEXUS_API_KEY:+--api-key $NEXUS_API_KEY}
