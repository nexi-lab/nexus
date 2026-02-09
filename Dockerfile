# Nexus RPC Server - Production Dockerfile
# Multi-stage build for optimal image size
# 国内镜像支持：APT、pip、Rust、Go
ARG USE_CHINA_MIRROR=false
FROM python:3.13-slim AS builder

# 设置国内镜像环境变量（默认 false，国外环境不使用）
ARG USE_CHINA_MIRROR
ENV USE_CHINA_MIRROR=${USE_CHINA_MIRROR}
ENV GOPROXY=https://goproxy.cn,direct
ENV GOSUMDB=off
# ---------- 系统依赖 ----------
RUN set -eux; \
    apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        git \
        curl \
        build-essential \
        ca-certificates \
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

# ---------- Go Toolchain ----------
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "arm64" ]; then GO_ARCH="arm64"; else GO_ARCH="amd64"; fi && \
    if [ "$USE_CHINA_MIRROR" = "true" ]; then \
        GO_DL="https://golang.google.cn/dl"; \
    else \
        GO_DL="https://go.dev/dl"; \
    fi && \
    curl -fsSL "${GO_DL}/go1.24.12.linux-${GO_ARCH}.tar.gz" | tar -C /usr/local -xzf -
ENV PATH="/usr/local/go/bin:/root/go/bin:${PATH}"

# ---------- Build Zoekt binaries ----------
RUN CGO_ENABLED=0 go install github.com/sourcegraph/zoekt/cmd/zoekt-index@latest && \
    CGO_ENABLED=0 go install github.com/sourcegraph/zoekt/cmd/zoekt-webserver@latest

# ---------- uv + maturin ----------
RUN if [ "$USE_CHINA_MIRROR" = "true" ]; then \
        PIP_INDEX="https://mirrors.cloud.tencent.com/pypi/simple"; \
    else \
        PIP_INDEX="https://pypi.org/simple"; \
    fi && \
    pip install --no-cache-dir -i $PIP_INDEX uv maturin

# ---------- Copy project files ----------
WORKDIR /build
COPY pyproject.toml uv.lock* README.md Cargo.toml ./
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic/alembic.ini ./alembic.ini

# ---------- Install Python dependencies ----------
ENV UV_HTTP_TIMEOUT=300
RUN if [ "$USE_CHINA_MIRROR" = "true" ]; then \
        PIP_INDEX="https://mirrors.cloud.tencent.com/pypi/simple"; \
    else \
        PIP_INDEX="https://pypi.org/simple"; \
    fi && \
    uv pip install --system -i $PIP_INDEX .

# ---------- Install sandbox providers ----------
RUN if [ "$USE_CHINA_MIRROR" = "true" ]; then \
        PIP_INDEX="https://mirrors.cloud.tencent.com/pypi/simple"; \
    else \
        PIP_INDEX="https://pypi.org/simple"; \
    fi && \
    uv pip install --system -i $PIP_INDEX docker e2b e2b-code-interpreter

# ---------- Build Rust extensions ----------
COPY rust/ ./rust/

# Build nexus_fast
WORKDIR /build/rust/nexus_fast
RUN maturin build --release && \
    pip install --no-cache-dir target/wheels/nexus_fast-*.whl

# Build nexus_raft
WORKDIR /build/rust/nexus_raft
RUN maturin build --release --features python && \
    pip install --no-cache-dir target/wheels/nexus_raft-*.whl

WORKDIR /build

# ---------- Production image ----------
FROM python:3.13-slim

ARG USE_CHINA_MIRROR
ENV USE_CHINA_MIRROR=${USE_CHINA_MIRROR}

# ---------- Runtime dependencies ----------
RUN set -eux; \
    apt-get update && apt-get install -y --no-install-recommends \
        curl \
        netcat-openbsd \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ---------- Copy Python packages + Rust extension ----------
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin/nexus /usr/local/bin/nexus
COPY --from=builder /usr/local/bin/alembic /usr/local/bin/alembic

# ---------- Copy Zoekt binaries ----------
COPY --from=builder /root/go/bin/zoekt-index /usr/local/bin/zoekt-index
COPY --from=builder /root/go/bin/zoekt-webserver /usr/local/bin/zoekt-webserver

# ---------- Optional verifications ----------
RUN python3 -c "import nexus_fast; print('✓ nexus_fast available')" || echo "⚠ nexus_fast not available"
RUN python3 -c "from _nexus_raft import LocalRaft; print('✓ nexus_raft available')" || echo "⚠ nexus_raft not available"
RUN python3 -c "import docker; print('✓ Docker Python package available')" || echo "⚠ Docker package not available"
RUN zoekt-index -h > /dev/null 2>&1 && echo "✓ Zoekt binaries available" || echo "⚠ Zoekt not available"

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
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NEXUS_HOST=0.0.0.0 \
    NEXUS_PORT=2026 \
    NEXUS_DATA_DIR=/app/data \
    ZOEKT_ENABLED=true \
    ZOEKT_URL=http://localhost:6070 \
    ZOEKT_INDEX_DIR=/app/data/.zoekt-index \
    ZOEKT_DATA_DIR=/app/data

EXPOSE 2026 6070

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${NEXUS_PORT}/health || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
