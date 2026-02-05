# Nexus LangGraph Agent Server - Production Dockerfile
# Uses nexus-langgraph repository instead of examples/langgraph
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

# Copy project files from nexus-langgraph
# Build context is set to nexi-lab/ (parent of both nexus/ and nexus-langgraph/)
WORKDIR /app
COPY nexus-langgraph/pyproject.toml ./
COPY nexus-langgraph/langgraph.json ./
COPY nexus-langgraph/agents ./agents
COPY nexus-langgraph/shared ./shared

# Install dependencies (nexus-fs-python from PyPI, not local build)
# Pin langgraph-cli to a known working version to avoid compatibility issues
# Recent versions have SDK/API mismatches causing startup failures
RUN uv pip install --system . "langgraph-cli==0.1.77"

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
COPY --from=builder /usr/local/bin/ /usr/local/bin/

# Copy application files
WORKDIR /app
COPY nexus-langgraph/langgraph.json ./
COPY nexus-langgraph/agents ./agents
COPY nexus-langgraph/shared ./shared

# Create non-root user for security
RUN useradd -r -m -u 1000 -s /bin/bash nexus && \
    chown -R nexus:nexus /app

# Create .langgraph_api directory with proper permissions
RUN mkdir -p /app/.langgraph_api && chown -R nexus:nexus /app/.langgraph_api

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
CMD ["sh", "-c", "exec langgraph dev --host ${LANGGRAPH_HOST} --port ${LANGGRAPH_PORT}"]
