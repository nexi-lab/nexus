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
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

    from nexus.contracts.types import OperationContext


# #4005 round-6: per-path refcounted lock pool. Round-5 used (zone_id,
# path) keys, but the path is *already* zone-scoped by the time it
# reaches occ_write_sync (scope_params_for_zone runs at the RPC layer,
# prepending ``/zone/<id>/`` for non-root callers). Adding caller
# zone_id to the key let a tenant-scoped caller and a root caller
# acquire different locks for the same canonical object, defeating the
# serialization. Key by the (already-canonicalized) path alone.
#
# Refcount cleanup: every entry is removed from the pool when no caller
# holds it. This bounds the pool to (concurrent OCC ops in flight) and
# closes the auth-DoS path where unauthorized callers spamming unique
# paths could grow the dict forever.
class _OccLockEntry:
    __slots__ = ("lock", "refs")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.refs = 0


_OCC_ENTRIES: dict[str, _OccLockEntry] = {}
_OCC_GUARD = threading.Lock()


@contextmanager
def _occ_path_lock(path: str) -> Iterator[None]:
    """Acquire the per-path OCC lock; release + GC the entry on exit."""
    with _OCC_GUARD:
        entry = _OCC_ENTRIES.get(path)
        if entry is None:
            entry = _OccLockEntry()
            _OCC_ENTRIES[path] = entry
        entry.refs += 1
    try:
        with entry.lock:
            yield
    finally:
        with _OCC_GUARD:
            entry.refs -= 1
            if entry.refs == 0:
                # Last holder leaves — drop the entry so the pool stays
                # bounded by live contention.
                _OCC_ENTRIES.pop(path, None)


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

    # #4005 round-7: canonicalize the path before keying the lock so
    # ``foo``, ``/foo``, ``/foo/``, ``//foo`` all resolve to the same
    # OCC entry. Without this two ``if_match`` writes targeting the same
    # storage object via different surface forms would acquire different
    # locks and both commit. Falls back to the raw path if validation
    # fails — sys_stat below will surface the same error.
    try:
        from nexus.core.path_utils import validate_path

        canonical_path = validate_path(path)
    except Exception:
        canonical_path = path

    with _occ_path_lock(canonical_path):
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
