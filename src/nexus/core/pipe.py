"""DT_PIPE kernel IPC primitive — SPSC message-oriented ring buffer (kfifo).

Implements the Kernel messaging tier from KERNEL-ARCHITECTURE.md §6:

    | Tier       | Linux Analogue   | Nexus                              | Latency |
    |------------|------------------|------------------------------------|---------|
    | **Kernel** | kfifo ring buffer| Nexus Native Pipe (DT_PIPE)        | ~0.5μs  |

This file defines the PipeBackend protocol and exception classes for DT_PIPE.
The actual data plane lives in the Rust kernel IPC registry
(DashMap<String, RingBufferCore>) inside ``nexus_kernel``. The mkfifo /
``fs/pipe.c`` equivalent (named-pipe creation, lookup, destroy) is also
owned by the Rust kernel — there is no Python ``PipeManager`` anymore.

    pipe.py = Python-side protocol + exceptions
    rust/kernel/src/ipc/pipe.rs = Rust kernel pipe registry (data plane)

Storage model (KERNEL-ARCHITECTURE.md line 228):
    - Pipe **inode** (FileMetadata, entry_type=DT_PIPE) → MetastoreABC
    - Pipe **data** (bytes in ring buffer) → Rust kernel IPC registry (not in any pillar)

See: federation-memo.md §7j
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PipeError(Exception):
    """Base exception for pipe operations."""


class PipeFullError(PipeError):
    """Non-blocking write on a full buffer."""


class PipeEmptyError(PipeError):
    """Non-blocking read on an empty buffer."""


class PipeClosedError(PipeError):
    """Operation on a closed pipe."""


class PipeNotFoundError(PipeError):
    """No pipe registered at the given path."""


class PipeExistsError(PipeError):
    """A pipe already exists at the given path."""


# ---------------------------------------------------------------------------
# PipeBackend protocol — pluggable transport tier
# ---------------------------------------------------------------------------


@runtime_checkable
class PipeBackend(Protocol):
    """Protocol for pipe data transport backends.

    Pluggable transport tier for DT_PIPE (KERNEL-ARCHITECTURE.md §4.2).
    PipeManager stores ``dict[str, PipeBackend]`` — all backends share
    this interface so PipeManager is transport-agnostic.

    **Concurrency contract**: All PipeBackend methods are **SPSC** (single-producer,
    single-consumer) with no internal synchronization. The asyncio event loop provides
    implicit serialization for coroutines, but this is a *usage property*, NOT a buffer
    guarantee. Multi-threaded callers MUST use PipeManager.pipe_write/pipe_read (which
    add per-pipe asyncio.Lock for MPMC safety).

    Implementations:
        Rust kernel IPC registry       — in-process SPSC ring buffer (Rust, ~0.5μs)
        SharedMemoryPipeBackend (shm_pipe.py) — cross-process mmap'd ring buffer (~1–5μs)
    """

    async def write(self, data: bytes, *, blocking: bool = True) -> int: ...
    async def read(self, *, blocking: bool = True) -> bytes: ...
    def write_nowait(self, data: bytes) -> int: ...
    def read_nowait(self) -> bytes: ...
    async def wait_writable(self) -> None: ...
    async def wait_readable(self) -> None: ...
    def close(self) -> None: ...

    @property
    def closed(self) -> bool: ...

    @property
    def stats(self) -> dict: ...


# ---------------------------------------------------------------------------
# Error translation (used by shm_pipe.py)
# ---------------------------------------------------------------------------


def _translate_rust_error(exc: RuntimeError) -> None:
    """Translate Rust RuntimeError tags to Python exception classes."""
    msg = str(exc)
    if msg.startswith("PipeClosed:"):
        raise PipeClosedError(msg.split(":", 1)[1]) from None
    if msg.startswith("PipeFull:"):
        raise PipeFullError(msg.split(":", 1)[1]) from None
    if msg.startswith("PipeEmpty:"):
        raise PipeEmptyError(msg.split(":", 1)[1]) from None
    # Unknown RuntimeError — re-raise as-is
    raise exc
