# E2B Template: Nexus FUSE (Rust) - High-performance FUSE client
FROM ubuntu:22.04

# Install dependencies
RUN apt-get update && apt-get install -y \
    curl \
    build-essential \
    pkg-config \
    libfuse-dev \
    fuse \
    sudo \
    && rm -rf /var/lib/apt/lists/*

# Install Rust with minimal profile
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal

# Copy source code
WORKDIR /build
ARG CACHEBUST=1
RUN echo "Cache bust: $CACHEBUST"
COPY Cargo.toml .
COPY src ./src

# Build release binary
RUN . $HOME/.cargo/env && cargo build --release

# Install binary
RUN cp /build/target/release/nexus-fuse /usr/local/bin/nexus-fuse && \
    chmod +x /usr/local/bin/nexus-fuse

# Clean up build artifacts to reduce image size
RUN rm -rf /build ~/.cargo/registry ~/.rustup

# Verify binary works
RUN nexus-fuse version

# Create user for E2B
RUN useradd -m -s /bin/bash user && \
    echo "user ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

USER user
WORKDIR /home/user
