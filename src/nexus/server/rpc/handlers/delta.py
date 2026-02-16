"""Delta sync RPC handler functions (Issue #869).

Extracted from fastapi_server.py (#1602). Provides rsync-style incremental
file updates via binary diffs (bsdiff4).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)


def handle_delta_read(nexus_fs: NexusFS, params: Any, context: Any) -> dict[str, Any]:
    """Handle delta_read method for rsync-style incremental updates.

    If client provides a content hash matching their cached version,
    returns only the delta (binary diff) instead of full file content.
    This reduces bandwidth by 50-90% for files with small changes.

    Args:
        nexus_fs: NexusFS instance
        params.path: File path to read
        params.client_hash: Client's current content hash (optional)
        params.max_delta_ratio: Max delta/original size ratio before falling back (default: 0.8)

    Returns:
        - If client_hash matches server: {"unchanged": True, "server_hash": ...}
        - If delta is smaller than threshold: {"delta": bytes, "server_hash": ..., "is_full": False}
        - If delta is larger or no client_hash: {"content": bytes, "server_hash": ..., "is_full": True}
    """
    import bsdiff4

    from nexus.core.hash_fast import hash_content

    # Read current file content
    content = nexus_fs.read(params.path, context=context)
    if isinstance(content, dict):
        content = content.get("content", b"")
    if isinstance(content, str):
        content = content.encode("utf-8")
    assert isinstance(content, bytes)

    server_hash = hash_content(content)

    client_hash = getattr(params, "client_hash", None)
    max_delta_ratio = getattr(params, "max_delta_ratio", 0.8)

    if client_hash is None:
        return {
            "content": content,
            "server_hash": server_hash,
            "is_full": True,
            "size": len(content),
        }

    if client_hash == server_hash:
        return {
            "unchanged": True,
            "server_hash": server_hash,
        }

    client_content = getattr(params, "client_content", None)

    if client_content is None:
        return {
            "content": content,
            "server_hash": server_hash,
            "is_full": True,
            "size": len(content),
            "reason": "client_content_required",
        }

    if isinstance(client_content, str):
        client_content = client_content.encode("utf-8")

    if hash_content(client_content) != client_hash:
        return {
            "content": content,
            "server_hash": server_hash,
            "is_full": True,
            "size": len(content),
            "reason": "client_hash_mismatch",
        }

    delta = bsdiff4.diff(client_content, content)

    delta_ratio = len(delta) / len(content) if len(content) > 0 else 1.0

    if delta_ratio > max_delta_ratio:
        return {
            "content": content,
            "server_hash": server_hash,
            "is_full": True,
            "size": len(content),
            "reason": "delta_too_large",
            "delta_ratio": delta_ratio,
        }

    return {
        "delta": delta,
        "server_hash": server_hash,
        "is_full": False,
        "delta_size": len(delta),
        "original_size": len(content),
        "compression_ratio": 1.0 - delta_ratio,
    }


def handle_delta_write(nexus_fs: NexusFS, params: Any, context: Any) -> dict[str, Any]:
    """Handle delta_write method for rsync-style incremental updates.

    Client sends a binary delta patch instead of full file content.
    Server applies the patch to the current file version.

    Args:
        nexus_fs: NexusFS instance
        params.path: File path to write
        params.delta: Binary delta patch (bsdiff4 format)
        params.base_hash: Expected hash of current server content
        params.if_match: Optional ETag for optimistic concurrency

    Returns:
        {"bytes_written": int, "new_hash": str} on success
        {"error": str, "reason": str} on conflict
    """
    import bsdiff4

    from nexus.core.hash_fast import hash_content

    delta = params.delta
    if isinstance(delta, str):
        delta = delta.encode("latin-1")

    base_hash = getattr(params, "base_hash", None)
    if base_hash is None:
        raise ValueError("base_hash is required for delta_write")

    try:
        current_content = nexus_fs.read(params.path, context=context)
        if isinstance(current_content, dict):
            current_content = current_content.get("content", b"")
        if isinstance(current_content, str):
            current_content = current_content.encode("utf-8")
        assert isinstance(current_content, bytes)
    except Exception as e:
        raise ValueError("Cannot apply delta to non-existent file. Use write() instead.") from e

    current_hash = hash_content(current_content)
    if current_hash != base_hash:
        return {
            "error": "conflict",
            "reason": "base_hash_mismatch",
            "expected_hash": base_hash,
            "actual_hash": current_hash,
        }

    new_content = bsdiff4.patch(current_content, delta)

    kwargs: dict[str, Any] = {"context": context}
    if hasattr(params, "if_match") and params.if_match:
        kwargs["if_match"] = params.if_match

    bytes_written = nexus_fs.write(params.path, new_content, **kwargs)
    new_hash = hash_content(new_content)

    return {
        "bytes_written": bytes_written,
        "new_hash": new_hash,
        "patch_applied": True,
    }
