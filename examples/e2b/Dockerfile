# E2B Dockerfile for Nexus Integration
# This template provides a pre-configured E2B sandbox with Nexus and FUSE support

FROM e2bdev/code-interpreter:latest

# Install system dependencies
RUN apt-get update && apt-get install -y \
    python3.11 \
    python3-pip \
    fuse3 \
    && rm -rf /var/lib/apt/lists/*

# Install Nexus with FUSE support
RUN pip install --no-cache-dir nexus-ai-fs[fuse]

# Create default mount point
RUN mkdir -p /home/user/nexus

# Set working directory
WORKDIR /home/user

# Default command
CMD ["/bin/bash"]
