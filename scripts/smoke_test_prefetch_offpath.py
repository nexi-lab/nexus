"""Second smoke test — verify NEXUS_PREFETCH_RUST=0 actually flips ReadaheadManager
to the Python path (no Rust engine constructed).

This complements smoke_test_prefetch.py which proves the Rust path lights up.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"OK: {msg}", flush=True)


def main() -> None:
    # Replicate what operations.py does — read the env, pass the resolved
    # boolean to ReadaheadManager.
    val = os.environ.get("NEXUS_PREFETCH_RUST", "1") != "0"
    print(
        f"NEXUS_PREFETCH_RUST={os.environ.get('NEXUS_PREFETCH_RUST', '<unset>')} -> use_rust_engine={val}",
        flush=True,
    )

    spec_path = pathlib.Path("/usr/local/lib/python3.14/site-packages/nexus/fuse/readahead.py")
    spec = importlib.util.spec_from_file_location("nexus.fuse.readahead", spec_path)
    if spec is None or spec.loader is None:
        fail(f"could not load {spec_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ReadaheadConfig = mod.ReadaheadConfig
    ReadaheadManager = mod.ReadaheadManager

    file_bytes = b"\x37" * (32 * 1024)

    def reader(_p: str, offset: int, size: int) -> bytes:
        return file_bytes[offset : offset + size]

    cfg = ReadaheadConfig(
        enabled=True,
        block_size=4096,
        prefetch_workers=2,
        min_sequential_count=2,
        initial_window=8192,
        max_window=32768,
    )
    rm = ReadaheadManager(config=cfg, read_func=reader, use_rust_engine=val)
    if val:
        if rm._rust_engine is None:
            fail("use_rust_engine=True but _rust_engine is None")
        ok("use_rust_engine=True -> _rust_engine bound (Rust path active)")
    else:
        if rm._rust_engine is not None:
            fail("use_rust_engine=False but _rust_engine was constructed")
        ok("use_rust_engine=False -> _rust_engine is None (Python fallback active)")

    # Quick smoke read through whichever path is active.
    fh = 1
    rm.on_open(fh, "/x", file_size=len(file_bytes))
    for off in (0, 4096, 8192):
        rm.on_read(fh, "/x", off, 4096)
    rm.on_release(fh)
    ok(f"on_open/on_read/on_release round-trip succeeded ({'Rust' if val else 'Python'} path)")
    print("=== TOGGLE OK ===", flush=True)


if __name__ == "__main__":
    main()
