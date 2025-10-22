# E2B Dockerfile for Nexus Integration
# You can use most Debian-based base images
FROM ubuntu:22.04

# Install system dependencies
# Note: Using FUSE 2 (fuse and libfuse2) for compatibility with fusepy
RUN apt-get update && apt-get install -y \
    python3.11 \
    python3-pip \
    fuse \
    libfuse2 \
    libfuse-dev \
    sudo \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip and install Nexus with FUSE support
# Note: Must use python3.11 explicitly because nexus-ai-fs requires Python >=3.11
RUN python3.11 -m pip install --upgrade pip && \
    python3.11 -m pip install --no-cache-dir nexus-ai-fs fusepy

# Create user and mount point
RUN useradd -m -s /bin/bash user && \
    mkdir -p /home/user/nexus && \
    chown -R user:user /home/user

# Give user passwordless sudo for nexus commands
RUN echo "user ALL=(ALL) NOPASSWD: /usr/local/bin/nexus" >> /etc/sudoers.d/nexus && \
    chmod 0440 /etc/sudoers.d/nexus

# Enable user_allow_other in fuse.conf so mounted filesystems can be accessed by non-root
RUN echo "user_allow_other" >> /etc/fuse.conf

# Set working directory
WORKDIR /home/user

# Switch to user
USER user
