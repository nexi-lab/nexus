# E2B Dockerfile for Nexus Integration with FUSE support
# Built for E2B sandboxes to enable Nexus FUSE mounting
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies (excluding python3-pip to avoid conflicts with deadsnakes)
RUN apt-get update && apt-get install -y \
    software-properties-common \
    fuse \
    libfuse2 \
    libfuse-dev \
    pkg-config \
    sudo \
    curl \
    git \
    nodejs \
    npm \
    && add-apt-repository ppa:deadsnakes/ppa -y \
    && apt-get update && apt-get install -y \
    python3.13 \
    python3.13-venv \
    && rm -rf /var/lib/apt/lists/*

# Make python3.13 the default python3
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.13 1

# Install pip using ensurepip (avoids apt conflicts)
RUN python3 -m ensurepip --upgrade

# Install fusepy from git (more compatible)
RUN python3 -m pip install --no-cache-dir 'fusepy @ git+https://github.com/fusepy/fusepy.git'

# Install cryptography for Python 3.13 (avoid conflict with system package)
RUN python3 -m pip install --no-cache-dir --ignore-installed cryptography

# Install Nexus from GitHub (latest main branch)
RUN python3 -m pip install --no-cache-dir 'nexus-ai-fs @ git+https://github.com/nexi-lab/nexus.git@main'

# Verify installations
RUN python3 -c "import fuse; print('fusepy OK')" && \
    nexus --version && \
    echo "FUSE support verified"

# Create mount points and user (needed for non-root operations)
RUN useradd -m -s /bin/bash user || true && \
    mkdir -p /home/user/nexus /mnt/nexus && \
    chown -R user:user /home/user /mnt/nexus

# Give user passwordless sudo for ALL commands (needed for FUSE mount)
RUN echo "user ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers.d/nexus && \
    chmod 0440 /etc/sudoers.d/nexus

# Enable user_allow_other in fuse.conf so mounted filesystems can be accessed by non-root
RUN echo "user_allow_other" >> /etc/fuse.conf

# Set working directory
WORKDIR /home/user

# Switch to user
USER user

# Default command - keep container running
CMD ["sleep", "infinity"]
