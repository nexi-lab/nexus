# Lightweight Nexus server image for demo/shared presets.
# Installs from PyPI with semantic-search support (txtai + pgvector).
# Mirrors the proven skill-hub approach (Issue #2915).

FROM python:3.13-slim

ARG NEXUS_VERSION=latest

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/app/data/.cache/huggingface \
    NEXUS_HOST=0.0.0.0 \
    NEXUS_PROFILE=full \
    NEXUS_PORT=2026 \
    NEXUS_DATA_DIR=/app/data \
    SENTENCE_TRANSFORMERS_HOME=/app/data/.cache/sentence-transformers

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

RUN if [ "$NEXUS_VERSION" = "latest" ]; then \
      uv pip install --system "nexus-ai-fs[semantic-search]" "txtai[ann]>=9.0"; \
    else \
      uv pip install --system "nexus-ai-fs[semantic-search]==${NEXUS_VERSION}" "txtai[ann]>=9.0"; \
    fi

RUN useradd -r -m -u 1000 -s /bin/bash nexus \
    && mkdir -p /app/data \
    && chown -R nexus:nexus /app

USER nexus

EXPOSE 2026

CMD nexusd \
    --host ${NEXUS_HOST} \
    --port ${NEXUS_PORT} \
    --data-dir ${NEXUS_DATA_DIR} \
    ${NEXUS_PROFILE:+--profile $NEXUS_PROFILE} \
    ${NEXUS_DATABASE_URL:+--database-url $NEXUS_DATABASE_URL} \
    ${NEXUS_AUTH_TYPE:+--auth-type $NEXUS_AUTH_TYPE} \
    ${NEXUS_API_KEY:+--api-key $NEXUS_API_KEY}
