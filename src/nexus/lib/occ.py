"""Optimistic Concurrency Control (OCC) helper (Issue #1323).

Stateless utility that composes kernel primitives (sys_stat + write)
to provide application-level compare-and-swap semantics.

OCC is NOT a kernel concern — ``write(2)`` doesn't do CAS.
Applications use ``flock(2)`` + retry for concurrency control.
This helper provides the equivalent for NexusFS callers.

Usage (from RPC handlers, CLI, SDK):

    from nexus.lib.occ import occ_write

    result = occ_write(
        nexus_fs,
        path="/foo.txt",
        buf=b"new content",
        context=ctx,
        if_match="sha256:abc...",   # fail if etag doesn't match
    )

Analogous to ``libpthread`` composing ``futex(2)`` — not kernel-internal,
but a standard composition of kernel syscalls.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext


# #4005 round-5: per-(zone, path) lock pool so two worker threads cannot
# both observe the same pre-state and both commit. The lock pool is
# unbounded in the worst case, but each lock is tiny and only paths
# under active OCC contention live here. Cleanup on idle is intentional
# punted: a once-per-N writes prune is brittle and the steady-state
# memory cost is negligible compared to the correctness win. Cross-
# process atomicity still requires a backend constraint (Issue #1323).
_OCC_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_OCC_LOCKS_GUARD = threading.Lock()


def _occ_lock_for(path: str, zone_id: str) -> threading.Lock:
    """Return the threading.Lock for ``(zone_id, path)`` (creating on demand)."""
    key = (zone_id, path)
    lock = _OCC_LOCKS.get(key)
    if lock is not None:
        return lock
    with _OCC_LOCKS_GUARD:
        lock = _OCC_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _OCC_LOCKS[key] = lock
        return lock


def occ_write_sync(
    fs: Any,
    path: str,
    buf: bytes | str,
    *,
    context: OperationContext | None = None,
    if_match: str | None = None,
    if_none_match: bool = False,
    offset: int = 0,
) -> dict[str, Any]:
    """Write with OCC pre-check (compare-and-swap).

    Composes ``sys_stat()`` + ``write()`` to provide etag-based
    optimistic concurrency control.

    Args:
        fs: NexusFS instance (or any object with sys_stat + write).
        path: Virtual file path.
        buf: File content.
        context: Operation context for permission checks.
        if_match: Expected etag — raises ConflictError on mismatch.
        if_none_match: If True, fail if file already exists (create-only).
        offset: POSIX pwrite offset (R20.10). 0 = full-file write.

    Returns:
        Dict with metadata (etag, version, modified_at, size) from write().

    Raises:
        FileExistsError: if_none_match=True and file exists.
        ConflictError: if_match provided and etag doesn't match.
    """
    # #4005 round-5: serialize the stat+write across worker threads
    # via a per-(zone, path) lock. Without this, two callers offloaded
    # via ``asyncio.to_thread`` could both observe the same pre-state
    # in different threads and both commit (lost updates / double-create
    # under if_match / if_none_match). Same-process serialization only;
    # cross-process atomicity still requires backend constraints.
    if if_match is None and not if_none_match:
        # Plain write — no compare phase, no lock needed.
        plain: dict[str, Any] = fs.write(path, buf, context=context, offset=offset)
        return plain

    from nexus.contracts.exceptions import ConflictError

    zone_id = getattr(context, "zone_id", None) or "_root"
    with _occ_lock_for(path, str(zone_id)):
        meta = fs.sys_stat(path, context=context)

        if if_none_match and meta is not None:
            raise FileExistsError(f"File already exists: {path}")

        if if_match is not None:
            if meta is None:
                raise ConflictError(
                    path=path,
                    expected_content_id=if_match,
                    current_content_id="(file does not exist)",
                )
            current_content_id = (
                meta.get("content_id")
                if isinstance(meta, dict)
                else getattr(meta, "content_id", None)
            )
            if current_content_id != if_match:
                raise ConflictError(
                    path=path,
                    expected_content_id=if_match,
                    current_content_id=current_content_id or "(no content_id)",
                )

        result: dict[str, Any] = fs.write(path, buf, context=context, offset=offset)
        return result


# #4005 round-3: ``occ_write`` was the original async name. Keep it as a
# thin async-friendly shim that offloads the whole sync compare-and-write
# inside one ``to_thread`` so the check + write run atomically (no await
# between them) AND the asyncio loop never blocks on the sync call.
async def occ_write(
    fs: Any,
    path: str,
    buf: bytes | str,
    *,
    context: OperationContext | None = None,
    if_match: str | None = None,
    if_none_match: bool = False,
    offset: int = 0,
) -> dict[str, Any]:
    """Async wrapper around :func:`occ_write_sync` (offloaded to a thread)."""
    import asyncio

    return await asyncio.to_thread(
        occ_write_sync,
        fs,
        path,
        buf,
        context=context,
        if_match=if_match,
        if_none_match=if_none_match,
        offset=offset,
    )
