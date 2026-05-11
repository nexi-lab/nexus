"""Verifies ReadaheadManager routes to the Rust PrefetchEngine when enabled."""

import time

import pytest

from nexus.fuse.readahead import ReadaheadConfig, ReadaheadManager

# The Rust-dependent test below is skipped if the cdylib isn't built/installable
# in the current dev environment.  CI builds the wheel before running tests;
# local unbuilt checkouts (cargo-only, no maturin develop yet) skip cleanly.
# The fallback test (`test_rust_engine_disabled_by_default`) still runs because
# it only exercises the Python path.
try:
    from nexus_runtime import PrefetchEngine  # noqa: F401

    _HAVE_RUST_PREFETCH = True
except ImportError:
    _HAVE_RUST_PREFETCH = False


@pytest.fixture
def synthetic_file_bytes() -> bytes:
    return b"\x42" * (256 * 1024)


@pytest.mark.skipif(
    not _HAVE_RUST_PREFETCH,
    reason="nexus_runtime.PrefetchEngine not installed",
)
def test_rust_engine_serves_sequential_hits(synthetic_file_bytes: bytes) -> None:
    config = ReadaheadConfig(
        enabled=True,
        block_size=4096,
        prefetch_workers=2,
        min_sequential_count=2,
        initial_window=16 * 1024,
        max_window=128 * 1024,
        sequential_tolerance=0,
        max_blocks_per_trigger=4,
    )

    def read_func(path: str, offset: int, size: int) -> bytes:
        return synthetic_file_bytes[offset : offset + size]

    rm = ReadaheadManager(config=config, read_func=read_func, use_rust_engine=True)
    fh = 1
    rm.on_open(fh, "/synthetic", file_size=len(synthetic_file_bytes))

    # Two warm-up reads — no hits expected.
    assert rm.on_read(fh, "/synthetic", 0, 4096) is None
    assert rm.on_read(fh, "/synthetic", 4096, 4096) is None
    # Third read triggers prefetch; give it a moment to land.
    assert rm.on_read(fh, "/synthetic", 8192, 4096) is None
    time.sleep(0.2)
    # Subsequent read should be a hit.
    got = rm.on_read(fh, "/synthetic", 12288, 4096)
    assert got is not None
    assert got == synthetic_file_bytes[12288:16384]
    rm.on_release(fh)


def test_rust_engine_disabled_by_default(synthetic_file_bytes: bytes) -> None:
    """When use_rust_engine is omitted, the Rust path is not used."""
    config = ReadaheadConfig(enabled=True)

    def read_func(path: str, offset: int, size: int) -> bytes:
        return synthetic_file_bytes[offset : offset + size]

    rm = ReadaheadManager(config=config, read_func=read_func)  # default off
    assert rm._rust_engine is None
