"""Shared fixtures for backend wrapper tests."""

from nexus.backends.wrappers.compressed import is_zstd_available

# Re-export for use in skipif decorators
zstd_available = is_zstd_available()
