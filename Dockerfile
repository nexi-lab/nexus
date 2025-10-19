# Nexus RPC Server - Production Dockerfile
# Multi-stage build for optimal image size
FROM python:3.11-slim as builder

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install uv for faster dependency installation
RUN pip install --no-cache-dir uv

# Copy project files
WORKDIR /build
COPY pyproject.toml uv.lock* README.md ./
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini ./

# Install dependencies to system
RUN uv pip install --system -e .

# Production image
FROM python:3.11-slim

# Install runtime dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy Python packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin/nexus /usr/local/bin/nexus

# Copy application files
WORKDIR /app
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini pyproject.toml README.md ./

# Create non-root user for security
RUN useradd -r -m -u 1000 -s /bin/bash nexus

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
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${NEXUS_PORT}/health || exit 1

# Run the server
CMD nexus serve \
    --host ${NEXUS_HOST} \
    --port ${NEXUS_PORT} \
    --data-dir ${NEXUS_DATA_DIR} \
    ${NEXUS_API_KEY:+--api-key $NEXUS_API_KEY}
