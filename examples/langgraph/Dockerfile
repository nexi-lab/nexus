# Nexus LangGraph Agent Server - Production Dockerfile
# Multi-stage build for optimal image size

FROM python:3.11-slim AS builder

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv for faster dependency installation
RUN pip install --no-cache-dir uv

# Copy project files
WORKDIR /app
COPY pyproject.toml ./
COPY *.py ./
COPY langgraph.json ./

# Install dependencies to system (not editable for multi-stage build)
RUN uv pip install --system .

# ============================================
# Production image
# ============================================
FROM python:3.11-slim

# Install runtime dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy Python packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin/langgraph /usr/local/bin/langgraph

# Copy application files
WORKDIR /app
COPY *.py ./
COPY langgraph.json ./

# Create non-root user for security
RUN useradd -r -m -u 1000 -s /bin/bash nexus && \
    chown -R nexus:nexus /app

# Switch to non-root user
USER nexus

# Environment variables (can be overridden)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANGGRAPH_HOST=0.0.0.0 \
    LANGGRAPH_PORT=2024

# Expose port
EXPOSE 2024

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:${LANGGRAPH_PORT}/health || exit 1

# Run the LangGraph server
CMD ["sh", "-c", "langgraph dev --allow-blocking --host ${LANGGRAPH_HOST} --port ${LANGGRAPH_PORT}"]
