# syntax=docker/dockerfile:1
# Nexus RPC Server - Production Dockerfile
# Multi-stage build for optimal image size
# 国内镜像支持：APT、pip、Rust、Go
ARG USE_CHINA_MIRROR=false
ARG TORCH_VARIANT=cpu

# ---------- Stage 1: Build Zoekt binaries (independent, cached separately) ----------
FROM golang:1.24 AS zoekt-builder
ARG USE_CHINA_MIRROR
RUN if [ "$USE_CHINA_MIRROR" = "true" ]; then \
        go env -w GOPROXY=https://goproxy.cn,direct GOSUMDB=off; \
    fi
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 go install github.com/sourcegraph/zoekt/cmd/zoekt-index@latest && \
    CGO_ENABLED=0 go install github.com/sourcegraph/zoekt/cmd/zoekt-webserver@latest

# ---------- Stage 2: Build Python + Rust ----------
FROM python:3.13-slim AS builder

# 设置国内镜像环境变量（默认 false，国外环境不使用）
ARG USE_CHINA_MIRROR
ARG TORCH_VARIANT
ENV USE_CHINA_MIRROR=${USE_CHINA_MIRROR}

# ---------- 系统依赖 ----------
RUN set -eux; \
    apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        git \
        curl \
        build-essential \
        ca-certificates \
        protobuf-compiler \
    && rm -rf /var/lib/apt/lists/*

# ---------- Rust Toolchain ----------
# 使用国内 Rust 镜像加速（如果 USE_CHINA_MIRROR）
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"
RUN if [ "$USE_CHINA_MIRROR" = "true" ]; then \
        rustup update stable --no-self-update; \
        rustup set profile minimal; \
        rustup component add rustfmt clippy; \
    fi

# ---------- Compute pip index URL once (DRY) ----------
RUN if [ "$USE_CHINA_MIRROR" = "true" ]; then \
        echo "https://mirrors.cloud.tencent.com/pypi/simple" > /tmp/pip_index; \
    else \
        echo "https://pypi.org/simple" > /tmp/pip_index; \
    fi

# ---------- uv + maturin ----------
RUN pip install --no-cache-dir -i $(cat /tmp/pip_index) uv maturin

# ---------- Install 3rd-party Python dependencies (stable cache layer) ----------
# Copy only metadata — src/ changes won't invalidate this expensive layer
WORKDIR /build
COPY pyproject.toml uv.lock* README.md Cargo.toml Cargo.lock ./
# Create minimal package stub so setuptools can discover the package
RUN mkdir -p src/nexus && echo '__version__ = "0.0.0"' > src/nexus/__init__.py
ENV UV_HTTP_TIMEOUT=300
# Pre-install torch before txtai[ann] to control the variant.
# TORCH_VARIANT=cpu  → CPU-only wheels (~300 MB, no CUDA)
# TORCH_VARIANT=cuda → Default PyPI wheels with CUDA (~2 GB)
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=cache,target=/root/.cache/pip \
    if [ "$TORCH_VARIANT" = "cpu" ]; then \
        uv pip install --system --index-url https://download.pytorch.org/whl/cpu torch; \
    else \
        uv pip install --system -i $(cat /tmp/pip_index) torch; \
    fi && \
    uv pip install --system -i $(cat /tmp/pip_index) \
        ".[all,performance,compression,monitoring,docker,event-streaming,sentry]" \
        "txtai[ann]>=9.0" \
        "sentence-transformers>=5.3"

# ---------- Build Rust extensions (shared target dir for dep reuse) ----------
COPY proto/ ./proto/
COPY rust/ ./rust/

ENV CARGO_TARGET_DIR=/build/target
RUN --mount=type=cache,target=/root/.cargo/registry \
    --mount=type=cache,target=/root/.cargo/git \
    --mount=type=cache,target=/build/target \
    maturin build --release --out /build/dist -m rust/nexus_pyo3/Cargo.toml && \
    maturin build --release --features full --out /build/dist -m rust/nexus_raft/Cargo.toml && \
    pip install --no-cache-dir /build/dist/nexus_fast-*.whl /build/dist/nexus_raft-*.whl

# ---------- Copy real application source and reinstall local package ----------
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic/alembic.ini ./alembic.ini
RUN rm -rf src/*.egg-info build/ && \
    pip install --no-cache-dir --no-deps --force-reinstall .

# ---------- Production image ----------
FROM python:3.13-slim

ARG USE_CHINA_MIRROR
ENV USE_CHINA_MIRROR=${USE_CHINA_MIRROR}

# ---------- Runtime dependencies ----------
# libgomp1: OpenMP runtime required by txtai, scikit-learn, numpy (Issue #2946)
RUN set -eux; \
    apt-get update && apt-get install -y --no-install-recommends \
        curl \
        netcat-openbsd \
        ca-certificates \
        libgomp1 \
        gosu \
    && rm -rf /var/lib/apt/lists/*

# ---------- Copy Python packages + Rust extensions ----------
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin/nexus /usr/local/bin/nexus
COPY --from=builder /usr/local/bin/nexusd /usr/local/bin/nexusd
COPY --from=builder /usr/local/bin/alembic /usr/local/bin/alembic

# ---------- Copy Zoekt binaries ----------
COPY --from=zoekt-builder /go/bin/zoekt-index /usr/local/bin/zoekt-index
COPY --from=zoekt-builder /go/bin/zoekt-webserver /usr/local/bin/zoekt-webserver

# ---------- Build-time smoke tests (Issue #2946) ----------
# Verify critical native imports are installed correctly.
# On ARM64 (Apple Silicon Docker), PyTorch's libc10.so may fail with
# "cannot allocate memory in static TLS block" — a known glibc/TLS
# limitation on aarch64 (see pytorch/pytorch#76689, OpenContracts#230).
# This does NOT affect runtime (the server starts fine); it only affects
# this build-time import check. We split the check: non-torch imports
# are fatal, torch-dependent imports (txtai) are best-effort on ARM64.
RUN python3 -c "\
import nexus_fast; \
from _nexus_raft import Metastore; \
import pgvector; \
import docker; \
import fastembed; \
import psutil; \
print('✓ Core imports passed')"
RUN python3 -c "\
import txtai; \
print('✓ txtai/torch import passed')" \
    || echo '⚠ txtai import failed (expected on ARM64 Docker — runtime unaffected)'
RUN which zoekt-index > /dev/null && which zoekt-webserver > /dev/null && echo "✓ Zoekt binaries available"

# ---------- Copy application files ----------
WORKDIR /app
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic/alembic.ini pyproject.toml README.md ./
COPY configs/ ./configs/
# Copy scripts (includes provisioning and entrypoint helpers)
COPY scripts/ ./scripts/
# Ensure Python helper scripts are executable inside the image
RUN chmod +x /app/scripts/*.py || true
# Include bundled skills and data assets
COPY data/ ./data/
COPY dockerfiles/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY dockerfiles/entrypoint-helpers.sh /usr/local/bin/entrypoint-helpers.sh

# Make entrypoint and helpers executable
RUN chmod +x /usr/local/bin/docker-entrypoint.sh /usr/local/bin/entrypoint-helpers.sh

# ---------- Create non-root user ----------
RUN useradd -r -m -u 1000 -s /bin/bash nexus
RUN usermod -aG root nexus
RUN mkdir -p /app/data && chown -R nexus:nexus /app

USER nexus

# ---------- Environment variables ----------
# Prevent faiss SVE auto-detection crash on aarch64 in Docker containers
# where /proc or /sys may not expose CPU feature flags correctly.
# OMP_NUM_THREADS=1 avoids the OpenMP runtime conflict between faiss-cpu
# and PyTorch on ARM (libiomp5 vs libomp).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NEXUS_HOST=0.0.0.0 \
    NEXUS_PORT=2026 \
    NEXUS_PROFILE=full \
    NEXUS_DATA_DIR=/app/data \
    ZOEKT_ENABLED=true \
    ZOEKT_URL=http://localhost:6070 \
    ZOEKT_INDEX_DIR=/app/data/.zoekt-index \
    ZOEKT_DATA_DIR=/app/data \
    FAISS_OPT_LEVEL=generic \
    OMP_NUM_THREADS=1 \
    GLIBC_TUNABLES=glibc.rtld.optional_static_tls=16384 \
    NEXUS_TXTAI_RERANKER=cross-encoder/ms-marco-MiniLM-L-2-v2 \
    NEXUS_TXTAI_SPARSE=true

EXPOSE 2026 2126 6070

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:${NEXUS_PORT}/healthz/ready || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
