# Nexus Runtime Docker Image - DEVELOPMENT VERSION
#
# A pre-configured Docker image for Nexus sandboxes with:
# - Python 3.11 with data science packages (pandas, numpy, matplotlib, scikit-learn, etc.)
# - Node.js 20
# - Nexus CLI FROM LOCAL SOURCE (for testing optimizations)
# - Non-root user with sudo access
# - FUSE support for mounting Nexus filesystem
#
# Build:
#   docker build -f docker/nexus-runtime-dev.Dockerfile -t nexus-runtime:dev .
#
# Usage:
#   Used automatically by DockerSandboxProvider when image is set to nexus-runtime:dev
#   Or manually: docker run -it --cap-add SYS_ADMIN nexus-runtime:dev

FROM python:3.11-slim

# Metadata
ARG NEXUS_VERSION=dev
ARG BUILD_TIME
LABEL org.nexus.version="${NEXUS_VERSION}"
LABEL org.nexus.build-time="${BUILD_TIME}"
LABEL org.nexus.description="Nexus Runtime DEV - Code execution sandbox with FUSE support (built from local source)"

# Install system dependencies
RUN apt-get update && apt-get install -y \
    # FUSE for filesystem mounting (both v2 and v3 for compatibility)
    fuse3 \
    libfuse2 \
    # Utilities
    curl \
    git \
    sudo \
    # Build tools (for pip packages with C extensions)
    build-essential \
    # Node.js setup
    ca-certificates \
    gnupg \
    # OpenCV dependencies
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    # Audio processing (for soundfile)
    libsndfile1 \
    # Cleanup in same layer to reduce image size
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 20.x
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user with sudo access (UID 1000 for consistency)
# Password-less sudo is needed for FUSE mounting
RUN useradd -m -u 1000 -s /bin/bash nexus && \
    echo "nexus ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# Install Nexus CLI FROM LOCAL SOURCE instead of PyPI
# This allows testing FUSE optimizations before they're published
COPY . /tmp/nexus-source
RUN pip install --no-cache-dir '/tmp/nexus-source[fuse]' && \
    rm -rf /tmp/nexus-source

# Install data science and utility packages (matching E2B sandbox)
# These packages enable Python code execution for data analysis, ML, and visualization
RUN pip install --no-cache-dir \
    # Data manipulation and analysis
    pandas \
    numpy \
    scipy \
    # Machine learning
    scikit-learn \
    scikit-image \
    # Visualization
    matplotlib \
    seaborn \
    plotly \
    # Image processing
    opencv-python \
    # NLP
    nltk \
    spacy \
    textblob \
    # File format support
    openpyxl \
    python-docx \
    xlrd \
    # Scientific computing
    sympy \
    xarray \
    # Audio processing
    soundfile \
    # Testing
    pytest \
    # Utilities
    requests \
    urllib3 \
    pytz \
    tornado

# Create common directories
RUN mkdir -p /home/nexus/workspace \
    /home/nexus/.cache \
    /mnt/nexus \
    && chown -R nexus:nexus /home/nexus /mnt/nexus

# Switch to non-root user
USER nexus
WORKDIR /home/nexus/workspace

# Set up Python path for user packages
ENV PATH="/home/nexus/.local/bin:${PATH}"

# Verify installations
RUN python --version && \
    node --version && \
    npm --version && \
    nexus --version

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

# Default command - sleep infinity allows container to stay running
# The DockerSandboxProvider will execute commands via docker exec
CMD ["sleep", "infinity"]
