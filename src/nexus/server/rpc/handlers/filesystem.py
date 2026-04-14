"""Filesystem RPC handler functions.

Extracted from fastapi_server.py (#1602). Each handler accepts ``nexus_fs``
as an explicit parameter instead of reaching into the module-level global.

All sync handlers are wrapped with ``to_thread_with_timeout`` by the dispatch
layer — they MUST NOT call async code directly.
"""

import logging
from typing import TYPE_CHECKING, Any, cast

from nexus.contracts.backend_features import BackendFeature
from nexus.contracts.constants import ROOT_ZONE_ID
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
    nexus_fs: "NexusFS",
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

        # S3 or GCS connector with signed URL support
        if hasattr(backend, "has_feature") and backend.has_feature(BackendFeature.SIGNED_URL):
            from dataclasses import replace

            if context and hasattr(context, "backend_path"):
                context = replace(context, backend_path=backend_path)

            # S3 uses generate_presigned_url, GCS uses generate_signed_url
            # (method unification deferred to Phase 6 cleanup)
            generate_fn = getattr(
                backend,
                "generate_presigned_url",
                getattr(backend, "generate_signed_url", None),
            )
            if generate_fn is None:
                raise RuntimeError("Backend declares SIGNED_URL but has no signed URL method")
            result = generate_fn(backend_path, expires_in, context)
            return {
                "download_url": result["url"],
                "expires_in": result["expires_in"],
                "method": result["method"],
                "backend": getattr(backend, "name", "unknown"),
            }

        # Local backend - use streaming endpoint with signed token
        if hasattr(backend, "has_feature") and backend.has_feature(BackendFeature.ROOT_PATH):
            from urllib.parse import quote

            from nexus.server.streaming import _sign_stream_token

            zone_id = ROOT_ZONE_ID
            if context and hasattr(context, "zone_id"):
                zone_id = context.zone_id or ROOT_ZONE_ID

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


async def handle_read_async(
    nexus_fs: "NexusFS", params: Any, context: Any
) -> bytes | dict[str, Any]:
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

    # Plain sys_read (Tier 1) — no metadata, no parsing
    if not parsed and not return_metadata:
        _count = getattr(params, "count", None)
        _offset = getattr(params, "offset", 0) or 0
        read_result: bytes = nexus_fs.sys_read(
            params.path,
            count=_count,
            offset=_offset,
            context=context,
        )
        return read_result

    # Read raw content via kernel
    read_result_rich: bytes | dict[str, Any] = nexus_fs.read(
        params.path,
        context=context,
        return_metadata=return_metadata,
    )

    # Apply parsed-content transform via ContentParserEngine (brick layer)
    if parsed:
        _engine = getattr(nexus_fs, "_parser_engine", None)
        if _engine is not None:
            raw = (
                read_result_rich["content"]
                if isinstance(read_result_rich, dict)
                else read_result_rich
            )
            content, parse_info = _engine.get_parsed_content(params.path, raw)
            if isinstance(read_result_rich, dict):
                read_result_rich["content"] = content
                read_result_rich["parsed"] = parse_info.get("parsed", False)
                read_result_rich["parse_provider"] = parse_info.get("provider")
                read_result_rich["parse_cached"] = parse_info.get("cached", False)
            else:
                read_result_rich = content

    if isinstance(read_result_rich, dict):
        read_result_rich = unscope_internal_dict(read_result_rich, ["path", "virtual_path"])
    return read_result_rich


# ---------------------------------------------------------------------------
# Write / mutate handlers
# ---------------------------------------------------------------------------


async def handle_write(nexus_fs: "NexusFS", params: Any, context: Any) -> dict[str, Any]:
    """Handle write method.

    OCC checks (if_match, if_none_match) are done here at the RPC layer
    via ``occ_write()`` helper — not in the kernel. Distributed locking
    is also NOT in write() — callers should use lock()/unlock() or
    ``async with locked(path)`` explicitly.

    Issue #1323: OCC + lock extracted from kernel.
    """
    content = getattr(params, "content", None) or getattr(params, "buf", None) or b""
    if isinstance(content, str):
        content = content.encode("utf-8")

    # OCC: use lib/occ helper if CAS params present
    if_match = getattr(params, "if_match", None) or None
    if_none_match = getattr(params, "if_none_match", False)
    force = getattr(params, "force", False)

    if (if_match or if_none_match) and not force:
        from nexus.lib.occ import occ_write

        write_result = await occ_write(
            nexus_fs,
            params.path,
            content,
            context=context,
            if_match=if_match,
            if_none_match=if_none_match,
        )
    else:
        write_result = nexus_fs.write(params.path, content, context=context)

    # write() returns dict with metadata (etag, version, modified_at, size).
    # Merge bytes_written into the response for backward compatibility.
    result: dict[str, Any] = {"bytes_written": len(content)}
    if isinstance(write_result, dict):
        result.update(write_result)
    return result


async def handle_exists(nexus_fs: "NexusFS", params: Any, context: Any) -> dict[str, Any]:
    """Handle exists method."""
    return {"exists": nexus_fs.access(params.path, context=context)}


async def handle_list(nexus_fs: "NexusFS", params: Any, context: Any) -> dict[str, Any]:
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
    search = nexus_fs.service("search")
    if search is not None:
        result = search.list(path=params.path, **kwargs)
    else:
        result = nexus_fs.sys_readdir(params.path, **kwargs)
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


async def handle_delete(nexus_fs: "NexusFS", params: Any, context: Any) -> dict[str, Any]:
    """Handle delete method."""
    try:
        nexus_fs.sys_unlink(params.path, context=context)
    except TypeError:
        nexus_fs.sys_unlink(params.path)
    return {"deleted": True}


async def handle_rename(nexus_fs: "NexusFS", params: Any, context: Any) -> dict[str, Any]:
    """Handle rename method."""
    try:
        nexus_fs.sys_rename(params.old_path, params.new_path, context=context)
    except TypeError:
        nexus_fs.sys_rename(params.old_path, params.new_path)
    return {"renamed": True}


def handle_copy(nexus_fs: "NexusFS", params: Any, context: Any) -> dict[str, Any]:
    """Handle copy method."""
    cast(Any, nexus_fs).copy(params.src_path, params.dst_path, context=context)
    return {"copied": True}


async def handle_mkdir(nexus_fs: "NexusFS", params: Any, context: Any) -> dict[str, Any]:
    """Handle mkdir method."""
    kwargs: dict[str, Any] = {"context": context}
    if hasattr(params, "parents") and params.parents is not None:
        kwargs["parents"] = params.parents
    if hasattr(params, "exist_ok") and params.exist_ok is not None:
        kwargs["exist_ok"] = params.exist_ok

    nexus_fs.mkdir(params.path, **kwargs)
    return {"created": True}


async def handle_rmdir(nexus_fs: "NexusFS", params: Any, context: Any) -> dict[str, Any]:
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


async def handle_get_metadata(nexus_fs: "NexusFS", params: Any, context: Any) -> dict[str, Any]:
    """Handle get_metadata method."""
    metadata = nexus_fs.sys_stat(params.path, context=context)
    if isinstance(metadata, dict):
        metadata = unscope_internal_dict(metadata, ["path"])
    return {"metadata": metadata}


async def handle_set_metadata(nexus_fs: "NexusFS", params: Any, context: Any) -> dict[str, Any]:
    """Handle set_metadata — persist metadata from RemoteMetastore.put().

    Reconstructs a FileMetadata from the dict sent by the client and
    stores it via the server-side metastore.
    """
    from nexus.storage._metadata_mapper_generated import MetadataMapper

    meta_dict: dict[str, Any] = params.metadata or {}
    # Ensure path is set (prefer params.path over dict contents)
    meta_dict["path"] = params.path

    file_meta = MetadataMapper.from_json(meta_dict)
    nexus_fs.metadata.put(file_meta)
    return {"path": params.path, "ok": True}


def handle_glob(nexus_fs: "NexusFS", params: Any, context: Any) -> dict[str, Any]:
    """Handle glob method."""
    kwargs: dict[str, Any] = {"context": context}
    if hasattr(params, "path") and params.path:
        kwargs["path"] = params.path

    search = nexus_fs.service("search")
    assert search is not None, "SearchService required for glob"
    matches = search.glob(params.pattern, **kwargs)
    matches = [unscope_internal_path(m) if isinstance(m, str) else m for m in matches]
    return {"matches": matches}


async def handle_grep(nexus_fs: "NexusFS", params: Any, context: Any) -> dict[str, Any]:
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

    search = nexus_fs.service("search")
    assert search is not None, "SearchService required for grep"
    results = await search.grep(params.pattern, **kwargs)
    results = [unscope_result(r) for r in results]
    return {"results": results}


def handle_search(nexus_fs: "NexusFS", params: Any, context: Any) -> dict[str, Any]:
    """Handle search method."""
    kwargs: dict[str, Any] = {"context": context}
    if hasattr(params, "path") and params.path:
        kwargs["path"] = params.path
    if hasattr(params, "limit") and params.limit is not None:
        kwargs["limit"] = params.limit
    if hasattr(params, "search_type") and params.search_type:
        kwargs["search_type"] = params.search_type

    results = cast(Any, nexus_fs).search(params.query, **kwargs)
    return {"results": results}


async def handle_semantic_search_index(
    nexus_fs: "NexusFS", params: Any, _context: Any
) -> dict[str, Any]:
    """Index documents for semantic search via the SearchDaemon's txtai backend.

    Single indexing path: reads files via NexusFS, upserts through the daemon's
    txtai backend which stores embeddings + BM25 tokens in pgvector. No separate
    document_chunks table needed — txtai is the single source of truth.

    Falls back to the SearchService pipeline if the daemon is unavailable.
    """
    import logging as _logging

    _log = _logging.getLogger(__name__)

    path = getattr(params, "path", "/")
    recursive = getattr(params, "recursive", True)

    search = nexus_fs.service("search")
    if search is None:
        raise ValueError("SearchService required for semantic search")

    # Prefer single-path indexing through the SearchDaemon's txtai backend.
    daemon = getattr(search, "_search_daemon", None)
    if daemon is not None and getattr(daemon, "_backend", None) is not None:
        context = _context
        zone_id = getattr(context, "zone_id", None) or "root"

        # RPC may scope paths as /zone/{id}/...; DB stores unscoped virtual_path.
        db_path = unscope_internal_path(path)

        # Query file paths from the daemon's database connection
        paths_to_index: list[str] = []
        if hasattr(daemon, "_async_session") and daemon._async_session is not None:
            from sqlalchemy import text as sa_text

            async with daemon._async_session() as sess:
                if recursive:
                    # Match both the path itself (single file) and children (directory)
                    like_pattern = db_path.rstrip("/") + "/%"
                    result = await sess.execute(
                        sa_text(
                            "SELECT virtual_path FROM file_paths"
                            " WHERE virtual_path LIKE :like OR virtual_path = :exact"
                        ),
                        {"like": like_pattern, "exact": db_path},
                    )
                    paths_to_index = [r[0] for r in result.fetchall()]
                else:
                    paths_to_index = [db_path]
            _log.info(
                "semantic_search_index: found %d files under %s", len(paths_to_index), db_path
            )

        # Read content and build documents for txtai
        documents: list[dict[str, Any]] = []
        read_errors = 0
        total_chunks = 0
        for file_path in paths_to_index:
            try:
                content = nexus_fs.sys_read(file_path, context=context)
                if isinstance(content, bytes):
                    content_str = content.decode("utf-8", errors="replace")
                else:
                    content_str = content
                if content_str.strip():
                    doc_id = f"{zone_id}:{file_path}" if zone_id != "root" else file_path
                    documents.append({"id": doc_id, "text": content_str, "path": file_path})
            except Exception as read_err:
                read_errors += 1
                _log.warning("Skipping %s: %s", file_path, read_err)

        _log.info(
            "semantic_search_index: %d documents read (%d errors)", len(documents), read_errors
        )

        # Single upsert to txtai → pgvector (BM25 + dense embeddings + SPLADE)
        results: dict[str, int] = {}
        if documents:
            await daemon.index_documents(documents, zone_id=zone_id)
            # Estimate per-file chunk counts from content length (~2KB/chunk)
            for doc in documents:
                chunks = max(1, len(doc["text"]) // 2000)
                results[doc["path"]] = chunks
                total_chunks += chunks
            _log.info(
                "semantic_search_index: indexed %d docs (~%d chunks)", len(documents), total_chunks
            )

        return {"indexed": results, "total_files": len(documents), "total_chunks": total_chunks}

    # Fallback: SearchService pipeline (when daemon is unavailable)
    try:
        await search.ainitialize_semantic_search(nx=nexus_fs, record_store_engine=None)
    except Exception as e:
        raise ValueError(f"Semantic search could not be initialized: {e}") from e

    results = await search.semantic_search_index(path=path, recursive=recursive)
    total_chunks = 0
    for v in results.values():
        if isinstance(v, int):
            total_chunks += v
        elif isinstance(v, dict) and "chunks" in v:
            total_chunks += v["chunks"]
    return {"indexed": results, "total_files": len(results), "total_chunks": total_chunks}


async def handle_semantic_search(nexus_fs: "NexusFS", params: Any, _context: Any) -> dict[str, Any]:
    """Handle semantic_search method — natural language search via SQL fallback."""
    search = nexus_fs.service("search")
    if search is None:
        raise ValueError("SearchService not available")

    results = await search.semantic_search(
        query=params.query,
        path=getattr(params, "path", "/"),
        limit=getattr(params, "limit", 10),
        search_mode=getattr(params, "search_mode", "semantic"),
        context=_context,
    )
    return {"results": results}


async def handle_is_directory(nexus_fs: "NexusFS", params: Any, context: Any) -> dict[str, Any]:
    """Handle is_directory method."""
    return {"is_directory": nexus_fs.is_directory(params.path, context=context)}
