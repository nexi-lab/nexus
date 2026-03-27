"""Shared constants for the nexus-fs package."""

from __future__ import annotations

# Default size limit for operations that buffer entire files in memory.
# Used by copy (facade), cat_file (fsspec), and write buffer (fsspec).
# Individual call sites may override if their limits diverge.
DEFAULT_MAX_FILE_SIZE = 1 * 1024 * 1024 * 1024  # 1 GB

# Streaming copy chunk size (64 MB).
STREAMING_COPY_CHUNK_SIZE = 64 * 1024 * 1024  # 64 MB
