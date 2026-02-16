"""Filesystem RPC handler functions.

Extracted from fastapi_server.py (#1602). Each handler accepts ``nexus_fs``
as an explicit parameter instead of reaching into the module-level global.

All sync handlers are wrapped with ``to_thread_with_timeout`` by the dispatch
layer — they MUST NOT call async code directly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nexus.server.path_utils import (
    unscope_internal_dict,
    unscope_internal_path,
    unscope_result,
)

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Download URL generation
# ---------------------------------------------------------------------------


def generate_download_url(
    nexus_fs: NexusFS,
    path: str,
    context: Any,
    expires_in: int = 3600,
) -> dict[str, Any] | None:
    """Generate presigned/signed URL for direct download if backend supports it.

    Supported backends:
    - S3: Returns presigned URL for direct download from S3
    - GCS: Returns signed URL for direct download from GCS
    - Local: Returns streaming endpoint URL with signed token

    Args:
        nexus_fs: NexusFS instance
        path: Virtual file path
        context: Operation context
        expires_in: URL expiration time in seconds

    Returns:
        Dict with download_url, expires_in, method, backend if supported, None otherwise
    """
    try:
        route = nexus_fs.router.route(path)
        backend = route.backend
        backend_path = route.backend_path

        # S3 connector
        if hasattr(backend, "generate_presigned_url"):
            from dataclasses import replace

            if context and hasattr(context, "backend_path"):
                context = replace(context, backend_path=backend_path)
            result = backend.generate_presigned_url(backend_path, expires_in, context)
            return {
                "download_url": result["url"],
                "expires_in": result["expires_in"],
                "method": result["method"],
                "backend": "s3",
            }

        # GCS connector
        if hasattr(backend, "generate_signed_url"):
            from dataclasses import replace

            if context and hasattr(context, "backend_path"):
                context = replace(context, backend_path=backend_path)
            result = backend.generate_signed_url(backend_path, expires_in, context)
            return {
                "download_url": result["url"],
                "expires_in": result["expires_in"],
                "method": result["method"],
                "backend": "gcs",
            }

        # Local backend - use streaming endpoint with signed token
        if backend.has_root_path:
            from urllib.parse import quote

            from nexus.server.streaming import _sign_stream_token

            zone_id = "default"
            if context and hasattr(context, "zone_id"):
                zone_id = context.zone_id or "default"

            token = _sign_stream_token(path, expires_in, zone_id)
            encoded_path = quote(path.lstrip("/"), safe="")

            return {
                "download_url": f"/api/stream/{encoded_path}?token={token}&zone_id={zone_id}",
                "expires_in": expires_in,
                "method": "GET",
                "backend": "local",
            }

        return None

    except Exception as e:
        logger.warning(f"Failed to generate download URL for {path}: {e}")
        return None


# ---------------------------------------------------------------------------
# Read handlers
# ---------------------------------------------------------------------------


async def handle_read_async(nexus_fs: NexusFS, params: Any, context: Any) -> bytes | dict[str, Any]:
    """Handle read method (async version for parsed reads).

    Returns raw bytes which will be encoded by encode_rpc_message using
    the standard {__type__: 'bytes', data: ...} format.

    If return_url=True and the backend supports it (S3/GCS connectors),
    returns a presigned URL instead of file content for direct download.
    """
    from nexus.server.fastapi_server import to_thread_with_timeout

    return_metadata = getattr(params, "return_metadata", False) or False
    parsed = getattr(params, "parsed", False) or False
    return_url = getattr(params, "return_url", False) or False
    expires_in = getattr(params, "expires_in", 3600) or 3600

    # Handle return_url - generate presigned URL for direct download
    if return_url:
        result = await to_thread_with_timeout(
            generate_download_url, nexus_fs, params.path, context, expires_in
        )
        if result:
            return result

    # If not parsed, use sync read in thread with timeout
    if not parsed:
        read_result: bytes | dict[str, Any] = await to_thread_with_timeout(
            nexus_fs.read,
            params.path,
            context,
            return_metadata,
            False,
        )
        if isinstance(read_result, dict):
            read_result = unscope_internal_dict(read_result, ["path", "virtual_path"])
        return read_result

    # For parsed reads, read raw content with timeout first
    raw_result = await to_thread_with_timeout(
        nexus_fs.read,
        params.path,
        context,
        True,
        False,
    )

    content = raw_result.get("content", b"") if isinstance(raw_result, dict) else raw_result

    # Parse the content asynchronously
    if hasattr(nexus_fs, "_get_parsed_content_async"):
        parsed_content, parse_info = await nexus_fs._get_parsed_content_async(params.path, content)
    else:
        parsed_content, parse_info = await to_thread_with_timeout(
            nexus_fs._get_parsed_content, params.path, content
        )

    if return_metadata:
        result = {
            "content": parsed_content,
            "parsed": parse_info.get("parsed", False),
            "provider": parse_info.get("provider"),
            "cached": parse_info.get("cached", False),
        }
        if isinstance(raw_result, dict):
            result["etag"] = raw_result.get("etag")
            result["version"] = raw_result.get("version")
            result["modified_at"] = raw_result.get("modified_at")
            result["size"] = len(parsed_content)
        return result

    return parsed_content


def handle_read(nexus_fs: NexusFS, params: Any, context: Any) -> bytes | dict[str, Any]:
    """Handle read method (sync version - kept for compatibility)."""
    kwargs: dict[str, Any] = {"context": context}
    if hasattr(params, "return_metadata") and params.return_metadata is not None:
        kwargs["return_metadata"] = params.return_metadata
    if hasattr(params, "parsed") and params.parsed is not None:
        kwargs["parsed"] = params.parsed

    result = nexus_fs.read(params.path, **kwargs)
    if isinstance(result, bytes):
        return result
    if isinstance(result, dict):
        result = unscope_internal_dict(result, ["path", "virtual_path"])
    return result


# ---------------------------------------------------------------------------
# Write / mutate handlers
# ---------------------------------------------------------------------------


def handle_write(nexus_fs: NexusFS, params: Any, context: Any) -> dict[str, Any]:
    """Handle write method."""
    content = params.content
    if isinstance(content, str):
        content = content.encode("utf-8")

    kwargs: dict[str, Any] = {"context": context}
    if hasattr(params, "if_match") and params.if_match:
        kwargs["if_match"] = params.if_match
    if hasattr(params, "if_none_match") and params.if_none_match:
        kwargs["if_none_match"] = params.if_none_match
    if hasattr(params, "force") and params.force:
        kwargs["force"] = params.force
    lock_val = getattr(params, "lock", None)
    if lock_val:
        kwargs["lock"] = lock_val
    lock_timeout_val = getattr(params, "lock_timeout", None)
    if lock_timeout_val is not None and lock_timeout_val != 30.0:
        kwargs["lock_timeout"] = lock_timeout_val

    bytes_written = nexus_fs.write(params.path, content, **kwargs)
    return {"bytes_written": bytes_written}


def handle_exists(nexus_fs: NexusFS, params: Any, context: Any) -> dict[str, Any]:
    """Handle exists method."""
    return {"exists": nexus_fs.exists(params.path, context=context)}


def handle_list(nexus_fs: NexusFS, params: Any, context: Any) -> dict[str, Any]:
    """Handle list method with optional pagination support."""
    import time as _time

    _handle_start = _time.time()

    kwargs: dict[str, Any] = {"context": context}
    if hasattr(params, "show_parsed") and params.show_parsed is not None:
        kwargs["show_parsed"] = params.show_parsed
    if hasattr(params, "recursive") and params.recursive is not None:
        kwargs["recursive"] = params.recursive
    if hasattr(params, "details") and params.details is not None:
        kwargs["details"] = params.details

    limit = getattr(params, "limit", None)
    cursor = getattr(params, "cursor", None)

    if limit is not None:
        kwargs["limit"] = limit
    if cursor:
        kwargs["cursor"] = cursor

    _list_start = _time.time()
    result = nexus_fs.list(params.path, **kwargs)
    _list_elapsed = (_time.time() - _list_start) * 1000

    # Result is PaginatedResult when limit is provided
    if hasattr(result, "to_dict"):
        _build_start = _time.time()
        paginated = result.to_dict()
        items = [
            unscope_internal_dict(f, ["path", "virtual_path"])
            if isinstance(f, dict)
            else unscope_internal_path(f)
            for f in paginated["items"]
        ]
        response = {
            "files": items,
            "next_cursor": paginated["next_cursor"],
            "has_more": paginated["has_more"],
            "total_count": paginated.get("total_count"),
        }
        _build_elapsed = (_time.time() - _build_start) * 1000
        _total_elapsed = (_time.time() - _handle_start) * 1000
        logger.info(
            f"[HANDLE-LIST] path={params.path}, list={_list_elapsed:.1f}ms, "
            f"build={_build_elapsed:.1f}ms, total={_total_elapsed:.1f}ms, "
            f"files={len(items)}, has_more={paginated['has_more']}"
        )
        return response

    # Fallback for non-paginated result
    _build_start = _time.time()
    raw_entries = result if isinstance(result, list) else []
    entries = [
        unscope_internal_dict(f, ["path", "virtual_path"])
        if isinstance(f, dict)
        else unscope_internal_path(f)
        for f in raw_entries
    ]
    response = {"files": entries, "has_more": False, "next_cursor": None}
    _build_elapsed = (_time.time() - _build_start) * 1000
    _total_elapsed = (_time.time() - _handle_start) * 1000
    logger.info(
        f"[HANDLE-LIST] path={params.path}, list={_list_elapsed:.1f}ms, "
        f"build={_build_elapsed:.1f}ms, total={_total_elapsed:.1f}ms, files={len(entries)}"
    )
    return response


def handle_delete(nexus_fs: NexusFS, params: Any, context: Any) -> dict[str, Any]:
    """Handle delete method."""
    try:
        nexus_fs.delete(params.path, context=context)
    except TypeError:
        nexus_fs.delete(params.path)
    return {"deleted": True}


def handle_rename(nexus_fs: NexusFS, params: Any, context: Any) -> dict[str, Any]:
    """Handle rename method."""
    try:
        nexus_fs.rename(params.old_path, params.new_path, context=context)
    except TypeError:
        nexus_fs.rename(params.old_path, params.new_path)
    return {"renamed": True}


def handle_copy(nexus_fs: NexusFS, params: Any, context: Any) -> dict[str, Any]:
    """Handle copy method."""
    nexus_fs.copy(params.src_path, params.dst_path, context=context)  # type: ignore[attr-defined]
    return {"copied": True}


def handle_mkdir(nexus_fs: NexusFS, params: Any, context: Any) -> dict[str, Any]:
    """Handle mkdir method."""
    kwargs: dict[str, Any] = {"context": context}
    if hasattr(params, "parents") and params.parents is not None:
        kwargs["parents"] = params.parents
    if hasattr(params, "exist_ok") and params.exist_ok is not None:
        kwargs["exist_ok"] = params.exist_ok

    nexus_fs.mkdir(params.path, **kwargs)
    return {"created": True}


def handle_rmdir(nexus_fs: NexusFS, params: Any, context: Any) -> dict[str, Any]:
    """Handle rmdir method."""
    kwargs: dict[str, Any] = {"context": context}
    if hasattr(params, "recursive") and params.recursive is not None:
        kwargs["recursive"] = params.recursive
    if hasattr(params, "force") and params.force is not None:
        kwargs["force"] = params.force

    nexus_fs.rmdir(params.path, **kwargs)
    return {"removed": True}


# ---------------------------------------------------------------------------
# Metadata / query handlers
# ---------------------------------------------------------------------------


def handle_get_metadata(nexus_fs: NexusFS, params: Any, context: Any) -> dict[str, Any]:
    """Handle get_metadata method."""
    metadata = nexus_fs.get_metadata(params.path, context=context)
    if isinstance(metadata, dict):
        metadata = unscope_internal_dict(metadata, ["path"])
    return {"metadata": metadata}


def handle_glob(nexus_fs: NexusFS, params: Any, context: Any) -> dict[str, Any]:
    """Handle glob method."""
    kwargs: dict[str, Any] = {"context": context}
    if hasattr(params, "path") and params.path:
        kwargs["path"] = params.path

    matches = nexus_fs.glob(params.pattern, **kwargs)
    matches = [unscope_internal_path(m) if isinstance(m, str) else m for m in matches]
    return {"matches": matches}


def handle_grep(nexus_fs: NexusFS, params: Any, context: Any) -> dict[str, Any]:
    """Handle grep method."""
    kwargs: dict[str, Any] = {"context": context}
    if hasattr(params, "path") and params.path:
        kwargs["path"] = params.path
    if hasattr(params, "ignore_case") and params.ignore_case is not None:
        kwargs["ignore_case"] = params.ignore_case
    if hasattr(params, "max_results") and params.max_results is not None:
        kwargs["max_results"] = params.max_results
    if hasattr(params, "file_pattern") and params.file_pattern is not None:
        kwargs["file_pattern"] = params.file_pattern
    if hasattr(params, "search_mode") and params.search_mode is not None:
        kwargs["search_mode"] = params.search_mode

    results = nexus_fs.grep(params.pattern, **kwargs)
    results = [unscope_result(r) for r in results]
    return {"results": results}


def handle_search(nexus_fs: NexusFS, params: Any, context: Any) -> dict[str, Any]:
    """Handle search method."""
    kwargs: dict[str, Any] = {"context": context}
    if hasattr(params, "path") and params.path:
        kwargs["path"] = params.path
    if hasattr(params, "limit") and params.limit is not None:
        kwargs["limit"] = params.limit
    if hasattr(params, "search_type") and params.search_type:
        kwargs["search_type"] = params.search_type

    results = nexus_fs.search(params.query, **kwargs)  # type: ignore[attr-defined]
    return {"results": results}


async def handle_semantic_search_index(
    nexus_fs: NexusFS, params: Any, _context: Any
) -> dict[str, Any]:
    """Handle semantic_search_index method."""
    path = getattr(params, "path", "/")
    recursive = getattr(params, "recursive", True)

    if not hasattr(nexus_fs, "_semantic_search") or nexus_fs._semantic_search is None:
        try:
            await nexus_fs.initialize_semantic_search()
        except Exception as e:
            raise ValueError(
                f"Semantic search is not initialized and could not be auto-initialized: {e}"
            ) from e

    results = await nexus_fs.semantic_search_index(path=path, recursive=recursive)

    total_chunks = 0
    for v in results.values():
        if isinstance(v, int):
            total_chunks += v
        elif isinstance(v, dict) and "chunks" in v:
            total_chunks += v["chunks"]

    return {"indexed": results, "total_files": len(results), "total_chunks": total_chunks}


def handle_is_directory(nexus_fs: NexusFS, params: Any, context: Any) -> dict[str, Any]:
    """Handle is_directory method."""
    return {"is_directory": nexus_fs.is_directory(params.path, context=context)}
