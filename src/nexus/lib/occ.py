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

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext


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
    if if_match is not None or if_none_match:
        from nexus.contracts.exceptions import ConflictError

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
