# Nexus LangGraph Agent Server - Production Dockerfile
# Multi-stage build for optimal image size

FROM python:3.13-slim AS builder

# Install build dependencies (including build-essential for Rust linking)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    git \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv for faster dependency installation
RUN pip install --no-cache-dir uv

# Copy parent nexus source code first (needed for local nexus-ai-fs installation)
WORKDIR /build
COPY src ./nexus-src/src
COPY rust ./nexus-src/rust
COPY Cargo.toml ./nexus-src/
COPY Cargo.lock ./nexus-src/
COPY pyproject.toml ./nexus-src/
COPY README.md ./nexus-src/
COPY uv.lock ./nexus-src/

# Install local nexus-ai-fs first
RUN uv pip install --system /build/nexus-src

# Copy examples/langgraph project files
WORKDIR /app
COPY examples/langgraph/pyproject.toml ./
COPY examples/langgraph/*.py ./
COPY examples/langgraph/langgraph.json ./

# Install dependencies (will skip nexus-ai-fs since already installed)
RUN uv pip install --system .

# ============================================
# Production image
# ============================================
FROM python:3.13-slim

# Install runtime dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy Python packages and CLI tools from builder
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin/langgraph /usr/local/bin/langgraph

# Copy application files
WORKDIR /app
COPY examples/langgraph/*.py ./
COPY examples/langgraph/langgraph.json ./

# Create non-root user for security
RUN useradd -r -m -u 1000 -s /bin/bash nexus && \
    chown -R nexus:nexus /app

# Switch to non-root user
USER nexus

# Environment variables (can be overridden)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    LANGGRAPH_HOST=0.0.0.0 \
    LANGGRAPH_PORT=2024

# Expose port
EXPOSE 2024

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:${LANGGRAPH_PORT}/ok || exit 1

# Tell Docker to send SIGINT instead of SIGTERM for graceful shutdown
# Uvicorn handles SIGINT properly and triggers the lifespan shutdown
STOPSIGNAL SIGINT

# Run the LangGraph server
# Use exec to make langgraph the PID 1 process (not sh)
CMD ["sh", "-c", "exec langgraph dev --allow-blocking --host ${LANGGRAPH_HOST} --port ${LANGGRAPH_PORT}"]
