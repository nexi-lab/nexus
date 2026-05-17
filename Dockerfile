# syntax=docker/dockerfile:1
# Nexus RPC Server - Production Dockerfile
# Multi-stage build for optimal image size
# 国内镜像支持：APT、pip、Rust、Go
ARG USE_CHINA_MIRROR=false

# Zoekt was previously built here as an independent Go stage and copied
# into the final image, but it's disabled by default (ZOEKT_ENABLED=false
# in compose) and nothing in the runtime requires it. Building the Go
# toolchain + cross-compiling for arm64 added ~600 MB to the multi-arch
# image and regularly flaked on transient module-proxy TLS timeouts
# (see develop CI run 24639873826). Dropped entirely — operators who
# want zoekt can install the binaries separately and bind-mount them
# into the container.

# ---------- Build Python + Rust ----------
FROM python:3.14-slim AS builder

# 设置国内镜像环境变量（默认 false，国外环境不使用）
ARG USE_CHINA_MIRROR
ARG TARGETARCH
ENV USE_CHINA_MIRROR=${USE_CHINA_MIRROR}

# ---------- 系统依赖 ----------
# protobuf-compiler is required by raft-proto v0.7.0's protobuf-build
# step.  The vendored protoc we wire up in our own kernel/raft build.rs
# only feeds tonic_build → prost-build; protobuf-build is a separate
# chain that shells out to ``protoc`` on PATH, so dropping this package
# broke every Docker build after commit 3d93e0155.
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

# ---------- uv ----------
RUN pip install --no-cache-dir -i $(cat /tmp/pip_index) uv

# ---------- pdf-inspector forward-compat (Issue #3757) ----------
# pdf-inspector 0.1.1 pins pyo3=0.25 which caps at Python 3.13. On 3.14+ we
# build from sdist and this env lets ABI3 forward-compat bypass the check.
# (Still needed for pip install of pdf-inspector from sdist.)
ENV PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1

# ---------- Install 3rd-party Python dependencies (stable cache layer) ----------
# Copy only metadata — src/ changes won't invalidate this expensive layer
WORKDIR /build
COPY pyproject.toml uv.lock* README.md Cargo.toml Cargo.lock ./
# Create minimal package stub so setuptools can discover the package
RUN mkdir -p src/nexus && echo '__version__ = "0.0.0"' > src/nexus/__init__.py
ENV UV_HTTP_TIMEOUT=300
# Select which pip extras to install at build time.
# Default (full image): all,performance,monitoring,docker,event-streaming,sentry,pay
# Lean sandbox image:   sandbox
# Issue #3699: torch / txtai / sentence-transformers / faiss-cpu / hnswlib
# all dropped — direct pgvector + pg_search path replaces them.
ARG NEXUS_PROFILE_EXTRAS=all,performance,monitoring,docker,event-streaming,sentry,pay
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=cache,target=/root/.cache/pip \
    uv pip install --system -i "$(cat /tmp/pip_index)" ".[${NEXUS_PROFILE_EXTRAS}]"

# ---------- Build Rust nexus-cluster binary (Issue #3125) ----------
COPY proto/ ./proto/
COPY rust/ ./rust/
# BuildKit cache mounts preserve Cargo target artifacts across Docker builds.
# Files copied into the image can be older than those cached artifacts, so
# Cargo may incorrectly consider a workspace crate fresh. Touch copied Rust
# sources so source changes cannot produce a stale binary.
RUN find rust proto -type f -exec touch {} +

ENV CARGO_TARGET_DIR=/build/target \
    CARGO_BUILD_JOBS=2 \
    CARGO_NET_RETRY=10 \
    CARGO_HTTP_TIMEOUT=120
RUN --mount=type=cache,target=/root/.cargo/registry \
    --mount=type=cache,target=/root/.cargo/git \
    --mount=type=cache,id=cargo-target-${TARGETARCH},target=/build/target \
    cargo build --release --manifest-path rust/profiles/cluster/Cargo.toml && \
    cp /build/target/release/nexusd-cluster /build/nexusd-cluster

# ---------- Copy real application source and reinstall local package ----------
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic/alembic.ini ./alembic.ini
RUN rm -rf src/*.egg-info build/ && \
    find /usr/local/lib/python3.*/site-packages/nexus/ -name "*.pyc" -delete 2>/dev/null; \
    find /usr/local/lib/python3.*/site-packages/nexus/ -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null; \
    pip install --no-cache-dir --no-deps --force-reinstall .

# ---------- Production image ----------
FROM python:3.14-slim

ARG USE_CHINA_MIRROR
ARG TARGETARCH
# Re-declare in stage-2 so smoke tests and conditional logic can read it.
ARG NEXUS_PROFILE_EXTRAS=all,performance,monitoring,docker,event-streaming,sentry,pay
ENV USE_CHINA_MIRROR=${USE_CHINA_MIRROR}
ENV NEXUS_PROFILE_EXTRAS=${NEXUS_PROFILE_EXTRAS}
# Python 3.14 + uvloop can leave Docker-launched Nexus accepting TCP
# connections without servicing HTTP requests on some local runtimes
# (Docker Desktop/OrbStack). Keep uvloop opt-in for containers.
ENV NEXUS_USE_UVLOOP=false \
    NEXUS_RATE_LIMIT_ENABLED=false \
    NEXUS_DEBUG_STACK_DUMP=false

# ---------- Runtime dependencies ----------
# libgomp1: OpenMP runtime required by scikit-learn / scipy / numpy.
RUN set -eux; \
    apt-get update && apt-get install -y --no-install-recommends \
        curl \
        netcat-openbsd \
        ca-certificates \
        libgomp1 \
        gosu \
    && rm -rf /var/lib/apt/lists/*

# ---------- CLI connectors: gws + gh (Issue #3148) ----------
# gws: Google Workspace CLI for Gmail/Calendar/Drive/Sheets/Docs/Chat connectors
# gh: GitHub CLI for GitHub connector
# Skipped for SANDBOX (#3778) — these connectors aren't used in the sandbox profile.
ARG TARGETARCH
# Retry wrapper: 3 attempts with exponential backoff for flaky external
# network steps (cli.github.com keyring, apt repo sync). Previously a
# transient curl/apt failure in --no-cache rebuilds aborted the whole
# image build (#3784 follow-up).
RUN set -eux; \
    retry() { \
        local n=0 max=3 delay=5; \
        until "$@"; do \
            n=$((n + 1)); \
            if [ "$n" -ge "$max" ]; then \
                echo "retry: giving up on: $*" >&2; \
                return 1; \
            fi; \
            echo "retry: attempt $n failed, sleeping ${delay}s before retry" >&2; \
            sleep "$delay"; \
            delay=$((delay * 2)); \
        done; \
    }; \
    case ",${NEXUS_PROFILE_EXTRAS}," in \
      *,all,*) \
        ARCH=$([ "${TARGETARCH}" = "arm64" ] && echo "aarch64" || echo "x86_64"); \
        tmpdir="$(mktemp -d)"; \
        trap 'rm -rf "$tmpdir"' EXIT; \
        retry curl -fsSL -o "$tmpdir/gws.tgz" \
            "https://github.com/googleworkspace/cli/releases/latest/download/google-workspace-cli-${ARCH}-unknown-linux-gnu.tar.gz"; \
        tar -xz -C "$tmpdir" -f "$tmpdir/gws.tgz"; \
        install -m 0755 "$tmpdir/gws" /usr/local/bin/gws; \
        sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources; \
        retry curl -fsSL -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
            https://cli.github.com/packages/githubcli-archive-keyring.gpg && \
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
            > /etc/apt/sources.list.d/github-cli.list && \
        retry apt-get update && \
        retry apt-get install -y --no-install-recommends git gh && \
        rm -rf /var/lib/apt/lists/* && \
        gws --version && gh --version ;; \
      *) echo "Skipping gws/gh CLI connectors for extras: ${NEXUS_PROFILE_EXTRAS}" ;; \
    esac

# ---------- Copy Python packages + Rust binary ----------
COPY --from=builder /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
COPY --from=builder /usr/local/bin/nexus /usr/local/bin/nexus
COPY --from=builder /usr/local/bin/nexusd /usr/local/bin/nexusd
COPY --from=builder /usr/local/bin/alembic /usr/local/bin/alembic
COPY --from=builder /build/nexusd-cluster /usr/local/bin/nexusd-cluster


# ---------- Build-time smoke tests (Issue #3125, #3134) ----------
# Verify nexusd-cluster binary is present and executable.
RUN nexusd-cluster --version || echo "nexusd-cluster binary OK"
# Extras-gated imports.
# SANDBOX profile deliberately excludes pgvector/docker/fastembed/psutil (Issue #3778).
RUN set -eux; \
    case ",${NEXUS_PROFILE_EXTRAS}," in \
      *,all,*) \
        python3 -c "import pgvector; import docker; import fastembed; import psutil; print('✓ all-extras imports passed')" ;; \
      *) echo "Skipping pgvector/docker/fastembed/psutil smoke test for extras: ${NEXUS_PROFILE_EXTRAS}" ;; \
    esac

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
    NEXUS_PROFILE=full \
    NEXUS_DATA_DIR=/app/data

EXPOSE 2026 2126

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl --max-time 5 -f http://localhost:${NEXUS_PORT}/health || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
