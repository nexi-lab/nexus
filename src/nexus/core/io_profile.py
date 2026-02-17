"""Per-mount I/O profile configuration (Issue #1413).

Defines IOProfile enum and IOProfileConfig frozen dataclass for tuning
readahead, write buffer, and cache priority on a per-mount basis.

Each mount can specify one of six profiles:
- FAST_READ:    Aggressive prefetch, high cache priority (model weights)
- FAST_WRITE:   Large write buffer, no readahead (conversation logs)
- EDIT:         Moderate readahead, sync writes (interactive editing)
- APPEND_ONLY:  No readahead, large async write buffer (append logs)
- BALANCED:     Default balanced settings for general workloads
- ARCHIVE:      Cold storage, all I/O features disabled
"""

from dataclasses import dataclass
from enum import StrEnum


class IOProfile(StrEnum):
    """I/O tuning profile for a mount point."""

    FAST_READ = "fast_read"
    FAST_WRITE = "fast_write"
    EDIT = "edit"
    APPEND_ONLY = "append_only"
    BALANCED = "balanced"
    ARCHIVE = "archive"

    def config(self) -> IOProfileConfig:
        """Return the IOProfileConfig for this profile."""
        return _PROFILE_CONFIGS[self]


@dataclass(frozen=True)
class IOProfileConfig:
    """Frozen configuration derived from an IOProfile.

    Three dimensions of tuning:
    1. Readahead — prefetch behavior for sequential reads
    2. Cache — L2 disk cache priority (0=minimal, 3=high)
    3. Write buffer — flush interval, max size, sync mode
       (data model only; VFS-level consumption deferred)
    """

    # Readahead
    readahead_enabled: bool
    readahead_initial_window: int  # bytes
    readahead_max_window: int  # bytes
    readahead_workers: int
    readahead_prefetch_on_open: bool

    # Cache
    cache_priority: int  # 0=minimal, 1=low, 2=medium, 3=high

    # Write buffer (data only — VFS consumption deferred)
    write_buffer_flush_interval_ms: int
    write_buffer_max_size: int  # MB
    write_buffer_sync_mode: bool  # True = sync writes, False = async


_PROFILE_CONFIGS: dict[IOProfile, IOProfileConfig] = {
    IOProfile.FAST_READ: IOProfileConfig(
        readahead_enabled=True,
        readahead_initial_window=512 * 1024,
        readahead_max_window=64 * 1024 * 1024,
        readahead_workers=8,
        readahead_prefetch_on_open=True,
        cache_priority=3,
        write_buffer_flush_interval_ms=100,
        write_buffer_max_size=50,
        write_buffer_sync_mode=False,
    ),
    IOProfile.FAST_WRITE: IOProfileConfig(
        readahead_enabled=False,
        readahead_initial_window=0,
        readahead_max_window=0,
        readahead_workers=0,
        readahead_prefetch_on_open=False,
        cache_priority=1,
        write_buffer_flush_interval_ms=50,
        write_buffer_max_size=500,
        write_buffer_sync_mode=False,
    ),
    IOProfile.EDIT: IOProfileConfig(
        readahead_enabled=True,
        readahead_initial_window=256 * 1024,
        readahead_max_window=1 * 1024 * 1024,
        readahead_workers=2,
        readahead_prefetch_on_open=False,
        cache_priority=2,
        write_buffer_flush_interval_ms=100,
        write_buffer_max_size=100,
        write_buffer_sync_mode=True,
    ),
    IOProfile.APPEND_ONLY: IOProfileConfig(
        readahead_enabled=False,
        readahead_initial_window=0,
        readahead_max_window=0,
        readahead_workers=0,
        readahead_prefetch_on_open=False,
        cache_priority=0,
        write_buffer_flush_interval_ms=500,
        write_buffer_max_size=1000,
        write_buffer_sync_mode=False,
    ),
    IOProfile.BALANCED: IOProfileConfig(
        readahead_enabled=True,
        readahead_initial_window=512 * 1024,
        readahead_max_window=32 * 1024 * 1024,
        readahead_workers=4,
        readahead_prefetch_on_open=True,
        cache_priority=2,
        write_buffer_flush_interval_ms=100,
        write_buffer_max_size=100,
        write_buffer_sync_mode=False,
    ),
    IOProfile.ARCHIVE: IOProfileConfig(
        readahead_enabled=False,
        readahead_initial_window=0,
        readahead_max_window=0,
        readahead_workers=0,
        readahead_prefetch_on_open=False,
        cache_priority=0,
        write_buffer_flush_interval_ms=0,
        write_buffer_max_size=0,
        write_buffer_sync_mode=False,
    ),
}
