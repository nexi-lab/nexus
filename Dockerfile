# Nexus Server - Cloud Run Dockerfile
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini ./

# Install uv for faster dependency installation
RUN pip install --no-cache-dir uv

# Install dependencies
RUN uv pip install --system -e .

# Create non-root user for security
RUN useradd -m -u 1000 nexus && chown -R nexus:nexus /app
USER nexus

# Environment variables (can be overridden)
ENV NEXUS_HOST=0.0.0.0
ENV NEXUS_PORT=8080
ENV PYTHONUNBUFFERED=1

# Expose port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/nexus').read()" || exit 1

# Run the server
CMD ["python", "-m", "nexus.server"]
