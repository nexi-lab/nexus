# syntax=docker/dockerfile:1
# Nexus RPC Server - Production Dockerfile
# Multi-stage build for optimal image size
# 国内镜像支持：APT、pip、Rust、Go
ARG USE_CHINA_MIRROR=false
ARG TORCH_VARIANT=cpu
ARG NEXUS_TXTAI_USE_API_EMBEDDINGS=false

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
ARG TORCH_VARIANT
ARG NEXUS_TXTAI_USE_API_EMBEDDINGS
ARG TARGETARCH
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

# ---------- pdf-inspector forward-compat (Issue #3757) ----------
# pdf-inspector 0.1.1 pins pyo3=0.25 which caps at Python 3.13. On 3.14+ we
# build from sdist and this env lets ABI3 forward-compat bypass the check.
ENV PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1

# ---------- Install 3rd-party Python dependencies (stable cache layer) ----------
# Copy only metadata — src/ changes won't invalidate this expensive layer
WORKDIR /build
COPY pyproject.toml uv.lock* README.md Cargo.toml Cargo.lock ./
# Create minimal package stub so setuptools can discover the package
RUN mkdir -p src/nexus && echo '__version__ = "0.0.0"' > src/nexus/__init__.py
ENV UV_HTTP_TIMEOUT=300
# Select which pip extras to install at build time.
# Default (full image): all,performance,compression,monitoring,docker,event-streaming,sentry,pay
# Lean sandbox image:   sandbox
ARG NEXUS_PROFILE_EXTRAS=all,performance,monitoring,docker,event-streaming,sentry,pay
# Pre-install torch ONLY when 'all' extras are selected — torch is ~300-2000 MB
# and is only consumed by txtai, which itself is gated on 'all'. SANDBOX (Issue #3778)
# and other lean extras skip it entirely.
# TORCH_VARIANT=cpu  → CPU-only wheels (~300 MB, no CUDA)
# TORCH_VARIANT=cuda → Default PyPI wheels with CUDA (~2 GB)
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=cache,target=/root/.cache/pip \
    set -eux; \
    case ",${NEXUS_PROFILE_EXTRAS}," in \
      *,all,*) \
        if [ "$TORCH_VARIANT" = "cpu" ]; then \
            uv pip install --system --index-url https://download.pytorch.org/whl/cpu torch; \
        else \
            uv pip install --system -i "$(cat /tmp/pip_index)" torch; \
        fi ;; \
      *) echo "Skipping torch pre-install for extras: ${NEXUS_PROFILE_EXTRAS}" ;; \
    esac; \
    uv pip install --system -i "$(cat /tmp/pip_index)" ".[${NEXUS_PROFILE_EXTRAS}]"; \
    case ",${NEXUS_PROFILE_EXTRAS}," in \
      *,all,*) \
        uv pip install --system -i "$(cat /tmp/pip_index)" "txtai[ann]>=9.0"; \
        if [ -z "${TARGETPLATFORM:-}" ] || [ "${TARGETPLATFORM:-}" = "linux/amd64" ]; then \
          uv pip install --system -i "$(cat /tmp/pip_index)" "sentence-transformers>=5.3"; \
        fi ;; \
      *) echo "Skipping txtai/sentence-transformers for profile extras: ${NEXUS_PROFILE_EXTRAS}" ;; \
    esac

# NOTE: hnswlib removal moved to after the final pip install (line ~121)
# to ensure it's not re-introduced by any subsequent install step.

# ---------- Build Rust extensions (Issue #3125) ----------
# On arm64, disable SimSIMD SVE backends at compile time. Apple Silicon does
# not implement SVE, and simsimd's runtime mrs-based SVE detection can misfire
# inside Docker Desktop's Virtualization.framework VM, causing SIGILL.
# Cache is scoped per TARGETARCH so amd64/arm64 builds never share artifacts.
COPY proto/ ./proto/
COPY rust/ ./rust/

ENV CARGO_TARGET_DIR=/build/target \
    CARGO_BUILD_JOBS=2 \
    CARGO_NET_RETRY=10 \
    CARGO_HTTP_TIMEOUT=120
RUN --mount=type=cache,target=/root/.cargo/registry \
    --mount=type=cache,target=/root/.cargo/git \
    --mount=type=cache,id=cargo-target-${TARGETARCH},target=/build/target \
    if [ "${TARGETARCH}" = "arm64" ]; then \
        export SIMSIMD_TARGET_SVE=0 \
               SIMSIMD_TARGET_SVE2=0 \
               SIMSIMD_TARGET_SVE_BF16=0 \
               SIMSIMD_TARGET_SVE_F16=0 \
               SIMSIMD_TARGET_SVE_I8=0; \
    fi && \
    maturin build --release --out /build/dist -m rust/kernel/Cargo.toml
RUN --mount=type=cache,target=/root/.cargo/registry \
    --mount=type=cache,target=/root/.cargo/git \
    --mount=type=cache,id=cargo-target-${TARGETARCH},target=/build/target \
    if [ "${TARGETARCH}" = "arm64" ]; then \
        export SIMSIMD_TARGET_SVE=0 \
               SIMSIMD_TARGET_SVE2=0 \
               SIMSIMD_TARGET_SVE_BF16=0 \
               SIMSIMD_TARGET_SVE_F16=0 \
               SIMSIMD_TARGET_SVE_I8=0; \
    fi && \
    maturin build --release --features full --out /build/dist -m rust/raft/Cargo.toml
RUN pip install --no-cache-dir /build/dist/nexus_kernel-*.whl /build/dist/nexus_raft-*.whl

# ---------- Copy real application source and reinstall local package ----------
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic/alembic.ini ./alembic.ini
RUN rm -rf src/*.egg-info build/ && \
    find /usr/local/lib/python3.*/site-packages/nexus/ -name "*.pyc" -delete 2>/dev/null; \
    find /usr/local/lib/python3.*/site-packages/nexus/ -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null; \
    pip install --no-cache-dir --no-deps --force-reinstall .

# On arm64, remove hnswlib LAST — after all pip installs are done.
# hnswlib 0.8.0's C extension executes SVE2 instructions that SIGILL on
# Apple Silicon Docker (M-chips do not implement SVE). txtai falls back
# gracefully to faiss/pgvector ANN without hnswlib.
ARG TARGETARCH
RUN if [ "${TARGETARCH}" = "arm64" ]; then \
        pip uninstall -y hnswlib && \
        rm -f /usr/local/lib/python3.14/site-packages/hnswlib*.so && \
        echo "✓ hnswlib removed (ARM64 SIGILL fix)"; \
    fi

# ---------- Production image ----------
FROM python:3.14-slim

ARG USE_CHINA_MIRROR
ARG TARGETARCH
# Re-declare in stage-2 so smoke tests and conditional logic can read it.
ARG NEXUS_PROFILE_EXTRAS=all,performance,monitoring,docker,event-streaming,sentry,pay
ENV USE_CHINA_MIRROR=${USE_CHINA_MIRROR}
ENV NEXUS_PROFILE_EXTRAS=${NEXUS_PROFILE_EXTRAS}

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

# PyTorch/txtai on slim Linux containers can fail with:
# "cannot allocate memory in static TLS block" for libgomp or libc10.
# Two-layer fix:
#   1. LD_PRELOAD: load the system libgomp + torch libc10 early so they
#      get TLS slots before the pool fills up.
#   2. GLIBC_TUNABLES: expand the static-TLS reservation so that
#      *additional* copies (e.g. faiss-cpu's bundled libgomp) still fit.
RUN set -eux; \
    if [ "${TARGETARCH}" = "arm64" ]; then \
        ln -sf /usr/lib/aarch64-linux-gnu/libgomp.so.1 /usr/lib/libgomp.so.1; \
    elif [ "${TARGETARCH}" = "amd64" ]; then \
        ln -sf /usr/lib/x86_64-linux-gnu/libgomp.so.1 /usr/lib/libgomp.so.1; \
    fi; \
    # torch is only installed when extras include 'all' — skip symlink on SANDBOX (#3778).
    if [ -f /usr/local/lib/python3.14/site-packages/torch/lib/libc10.so ]; then \
        ln -sf /usr/local/lib/python3.14/site-packages/torch/lib/libc10.so /usr/lib/libc10.so; \
    fi
# LD_PRELOAD: libgomp only (always safe). When torch is installed, the entrypoint
# extends LD_PRELOAD to include libc10.so (see docker-entrypoint.sh).
ENV LD_PRELOAD="/usr/lib/libgomp.so.1"
ENV GLIBC_TUNABLES="glibc.rtld.optional_static_tls=16384"

# ---------- CLI connectors: gws + gh (Issue #3148) ----------
# gws: Google Workspace CLI for Gmail/Calendar/Drive/Sheets/Docs/Chat connectors
# gh: GitHub CLI for GitHub connector
# Skipped for SANDBOX (#3778) — these connectors aren't used in the sandbox profile.
ARG TARGETARCH
RUN set -eux; \
    case ",${NEXUS_PROFILE_EXTRAS}," in \
      *,all,*) \
        ARCH=$([ "${TARGETARCH}" = "arm64" ] && echo "aarch64" || echo "x86_64"); \
        tmpdir="$(mktemp -d)"; \
        trap 'rm -rf "$tmpdir"' EXIT; \
        curl -fsSL "https://github.com/googleworkspace/cli/releases/latest/download/google-workspace-cli-${ARCH}-unknown-linux-gnu.tar.gz" \
            | tar -xz -C "$tmpdir"; \
        install -m 0755 "$tmpdir/gws" /usr/local/bin/gws; \
        sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources; \
        curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
            | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
            > /etc/apt/sources.list.d/github-cli.list && \
        apt-get update && apt-get install -y --no-install-recommends git gh && \
        rm -rf /var/lib/apt/lists/* && \
        gws --version && gh --version ;; \
      *) echo "Skipping gws/gh CLI connectors for extras: ${NEXUS_PROFILE_EXTRAS}" ;; \
    esac

# ---------- Copy Python packages + Rust extensions ----------
COPY --from=builder /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
COPY --from=builder /usr/local/bin/nexus /usr/local/bin/nexus
COPY --from=builder /usr/local/bin/nexusd /usr/local/bin/nexusd
COPY --from=builder /usr/local/bin/alembic /usr/local/bin/alembic


# ---------- Build-time smoke tests (Issue #2946, #3125, #3134) ----------
# Verify critical native imports are installed correctly.
# The SIMD test exercises simsimd code paths so that a cross-architecture
# cache mismatch or mis-compiled SVE backend surfaces as a build failure
# (SIGILL) instead of a runtime crash (Issue #3125).
# On ARM64 (Apple Silicon Docker), PyTorch's libc10.so may fail with
# "cannot allocate memory in static TLS block" — a known glibc/TLS
# limitation on aarch64 (see pytorch/pytorch#76689, OpenContracts#230).
# This does NOT affect runtime (the server starts fine); it only affects
# this build-time import check. We split the check: non-torch imports
# are fatal, torch-dependent imports (txtai) are best-effort on ARM64.
# Always verifiable (present regardless of extras): Rust extensions.
RUN python3 -c "\
import nexus_kernel; \
from _nexus_raft import Metastore; \
print('✓ Core imports passed (always-present subset)')"
# Extras-gated imports.
# SANDBOX profile deliberately excludes pgvector/docker/fastembed/psutil (Issue #3778).
RUN set -eux; \
    case ",${NEXUS_PROFILE_EXTRAS}," in \
      *,all,*) \
        python3 -c "import pgvector; import docker; import fastembed; import psutil; print('✓ all-extras imports passed')" ;; \
      *) echo "Skipping pgvector/docker/fastembed/psutil smoke test for extras: ${NEXUS_PROFILE_EXTRAS}" ;; \
    esac
RUN python3 -c "\
from nexus_kernel import cosine_similarity_f32, dot_product_f32; \
s = cosine_similarity_f32([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]); \
assert abs(s - 1.0) < 0.01, f'cosine self-similarity failed: {s}'; \
d = dot_product_f32([1.0, 2.0], [3.0, 4.0]); \
assert abs(d - 11.0) < 0.01, f'dot product failed: {d}'; \
print('✓ SIMD smoke test passed')"
RUN python3 -c "\
import txtai; \
print('✓ txtai/torch import passed')" \
    || echo '⚠ txtai import failed (expected on ARM64 Docker — runtime unaffected)'

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
# SIMD mitigations (FAISS_OPT_LEVEL, OMP_NUM_THREADS, MKL_ENABLE_INSTRUCTIONS)
# are applied as portable defaults at runtime in docker-entrypoint.sh to avoid
# faiss/torch SIGILL on diverse CPUs (Issue #3125).  Users with known-good
# modern CPUs can override via `docker run -e <VAR>=<value>` for full SIMD
# throughput.  GLIBC_TUNABLES is set unconditionally above.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NEXUS_HOST=0.0.0.0 \
    NEXUS_PORT=2026 \
    NEXUS_PROFILE=full \
    NEXUS_DATA_DIR=/app/data \
    NEXUS_TXTAI_RERANKER=cross-encoder/ms-marco-MiniLM-L-2-v2 \
    NEXUS_TXTAI_SPARSE=false

EXPOSE 2026 2126

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:${NEXUS_PORT}/healthz/ready || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
