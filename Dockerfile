# Nexus RPC Server - Production Dockerfile
# Multi-stage build for optimal image size
FROM python:3.13-slim as builder

# Install build dependencies (including Rust for nexus_fast extension)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    git \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Rust toolchain for building nexus_fast extension
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"

# Install uv and maturin for faster dependency installation
RUN pip install --no-cache-dir uv maturin

# Copy project files
WORKDIR /build
COPY pyproject.toml uv.lock* README.md Cargo.toml ./
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini ./

# Install dependencies to system (not editable for multi-stage build)
# Increase timeout for large packages like onnxruntime (14.5MB)
ENV UV_HTTP_TIMEOUT=300
RUN uv pip install --system .

# Install sandbox providers
RUN uv pip install --system docker e2b

# Build and install Rust extension (nexus_fast)
COPY rust/ ./rust/
WORKDIR /build/rust/nexus_fast
RUN maturin build --release && \
    pip install --no-cache-dir target/wheels/nexus_fast-*.whl
WORKDIR /build

# Production image
FROM python:3.13-slim

# Install runtime dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

# Copy Python packages from builder (including Rust extension)
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin/nexus /usr/local/bin/nexus
COPY --from=builder /usr/local/bin/alembic /usr/local/bin/alembic

# Verify Rust extension is available (optional debug step)
RUN python3 -c "import nexus_fast; print('✓ Rust acceleration available')" || echo "⚠ Rust not available"

# Verify Docker sandbox provider is available
RUN python3 -c "import docker; print('✓ Docker Python package available')" || echo "⚠ Docker package not available"

# Copy application files
WORKDIR /app
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini pyproject.toml README.md ./
COPY configs/ ./configs/
# Copy scripts (includes provisioning)
COPY scripts/ ./scripts/
# Include bundled skills and data assets
COPY data/ ./data/
COPY docker-entrypoint.sh /usr/local/bin/

# Make entrypoint executable
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Create non-root user for security
RUN useradd -r -m -u 1000 -s /bin/bash nexus

# Add nexus user to root group for Docker socket access
# This is safe in containers as the root group doesn't grant elevated privileges
RUN usermod -aG root nexus

# Create data directory with correct permissions
RUN mkdir -p /app/data && chown -R nexus:nexus /app

# Switch to non-root user
USER nexus

# Environment variables (can be overridden)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NEXUS_HOST=0.0.0.0 \
    NEXUS_PORT=8080 \
    NEXUS_DATA_DIR=/app/data

# Expose port
EXPOSE 8080

# Health check - updated to correct endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${NEXUS_PORT}/health || exit 1

# Run the server via entrypoint script
# The entrypoint handles database initialization and API key creation
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
