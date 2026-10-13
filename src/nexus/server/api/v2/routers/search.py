"""Explicit document indexing and per-file refresh adapters."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.runtime.zone_resolution import target_zone_for_context
from nexus.server.api.v2.error_handling import api_error_handler
from nexus.server.api.v2.routers._index_on_write import (
    REASON_EMPTY,
    REASON_NON_TEXT,
    STATUS_SKIPPED,
    build_document,
    decode_index_text,
    index_zone_for,
    read_written_bytes,
    verdict_for_path,
)
from nexus.server.api.v2.routers._search_deps import _get_search_daemon
from nexus.server.dependencies import get_operation_context, require_auth
from nexus.server.zone_execution import run_zone_scoped

router = APIRouter(prefix="/api/v2/search", tags=["search"])


def _get_zone_registry(request: Request) -> Any | None:
    return getattr(request.app.state, "zone_registry", None)


# =============================================================================
# Endpoints
# =============================================================================


@router.post("/index")
@api_error_handler(context="index Search documents", error_map={ValueError: (400, "{error}")})
async def search_index_documents(
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """Explicitly index documents (Decision #18: call-by-call indexing).

    Request body: ``{"documents": [{"id": str, "text": str, "path": str, ...}]}``

    The host authorizes management requests and commits indexing synchronously.
    Persistence failures return HTTP 500. Host-reported pending paths return
    HTTP 409 with ``detail.skipped``; completed documents remain indexed.
    """
    zone_id = index_zone_for(auth_result)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    documents = body.get("documents", [])
    if not isinstance(documents, list) or any(not isinstance(doc, dict) for doc in documents):
        raise HTTPException(status_code=400, detail="'documents' must be a list of objects")
    if not documents:
        raise HTTPException(status_code=400, detail="No documents provided")
    # Tenant boundary (review R2, CRITICAL): the request is authorized
    # for ONE zone, but each document dict may carry its own zone_id,
    # which the plugin treats as an override — a writer authorized for
    # zone A could inject documents into zone B's index.  Reject any
    # explicit mismatch loudly; matching/absent per-doc zones are
    # normalized to the authorized zone (the proxy also force-stamps
    # it as defense in depth).
    for doc in documents:
        for field in ("path", "text", "zone_id"):
            if field in doc and not isinstance(doc[field], str):
                raise HTTPException(status_code=400, detail=f"document '{field}' must be a string")
        doc_zone = doc.get("zone_id")
        if doc_zone and doc_zone != zone_id:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"document zone_id {doc_zone!r} does not match the authorized zone {zone_id!r}"
                ),
            )

    return await run_zone_scoped(
        _get_zone_registry(request),
        zone_id if zone_id != ROOT_ZONE_ID else None,
        lambda: _index_documents_work(search_daemon, documents, zone_id),
    )


async def _index_documents_work(
    search_daemon: Any, documents: list[dict[str, Any]], zone_id: str
) -> dict[str, Any]:
    """Index documents and preserve the host's commit result."""
    result = await search_daemon.index_documents(documents, zone_id=zone_id)
    count = result["indexed"]
    skipped = list(result.get("skipped") or [])
    skipped_count = int(result.get("skipped_count") or 0)
    skipped_paths = list(result.get("skipped_paths") or [])
    index_seq = result.get("index_seq")
    if skipped:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "documents were not indexed; resolve the skipped paths before retrying",
                "count": count,
                "skipped": skipped,
                "zone_id": zone_id,
            },
        )
    # ``zoneId`` casing is the pre-P12 public contract (#4617);
    # ``skippedCount`` is additive (review R2) — content-level
    # skips (empty/whitespace/chunkless docs) were previously
    # invisible behind a bare 200, so a caller couldn't tell
    # "all indexed" from "half the batch was empty".
    # #4736: ``skippedPaths`` names the content-skipped documents
    # behind ``skippedCount``; ``indexSeq`` is the plugin sequence
    # stamped after this batch's commit — ``/search/stats``
    # ``last_index_seq >= indexSeq`` means the batch is served.
    return {
        "status": "indexed",
        "count": int(count),
        "skippedCount": skipped_count,
        "skippedPaths": skipped_paths,
        "indexSeq": int(index_seq) if index_seq is not None else None,
        "zoneId": zone_id,
    }


@router.post("/refresh")
@api_error_handler(context="refresh Search document")
async def search_refresh_notify(
    request: Request,
    path: str = Query(..., description="Path of the changed file"),
    change_type: str = Query("update", description="Type of change: create, update, delete"),
    auth_result: dict[str, Any] = Depends(require_auth),
    search_daemon: Any = Depends(_get_search_daemon),
) -> dict[str, Any]:
    """Make ONE path searchable, or evict it — synchronously (#4736).

    * ``create`` / ``update`` — the server reads ``path`` through the
      VFS with the caller's context and indexes the text via
      ``IndexDocuments``.  200 ``{"status": "indexed", "index_seq": N}``
      once the plugin has committed; 404 when the path does not exist;
      409 ``{"status": "skipped", "reason": …}`` when there is nothing
      to index (empty / whitespace-only / non-UTF-8 content).  The
      plugin indexes synchronously, so there is no 202 "queued" outcome
      and no result is ever reported before it is served.
    * ``delete`` — ``NotifyFileChange`` drops the path's chunks and
      records a tombstone; 200 ``{"status": "deleted", "index_seq": N}``.

    The Rust host requires management authority for index mutations. Every
    plugin call is stamped with the token's zone — the zone
    ``/search/query`` reads and ``/search/index`` writes.
    """
    if change_type not in ("create", "update", "delete"):
        raise HTTPException(status_code=400, detail=f"invalid change_type {change_type!r}")

    op_context = get_operation_context(auth_result)
    # Zone RUNNER key (path-aware, matches the files routes) …
    target_zone = target_zone_for_context(op_context, {"path": path})
    # … vs the PLUGIN zone: the token's zone, shared with /search/index,
    # /search/query and write+index so the four surfaces cannot drift.
    index_zone = index_zone_for(auth_result)

    def _skipped(reason: str) -> HTTPException:
        # 409 rather than 200: "nothing to index" must be distinguishable
        # from "indexed" without the caller diffing a follow-up query.
        return HTTPException(
            status_code=409,
            detail={
                "status": STATUS_SKIPPED,
                "path": path,
                "change_type": change_type,
                "reason": reason,
            },
        )

    async def _work() -> dict[str, Any]:
        if change_type == "delete":
            outcome = await search_daemon.notify_file_change(path, "delete", zone_id=index_zone)
            index_seq = outcome.get("index_seq")
            return {
                "status": "deleted",
                "path": path,
                "change_type": change_type,
                "index_seq": index_seq,
            }

        nexus_fs = getattr(request.app.state, "nexus_fs", None)
        if nexus_fs is None:
            raise HTTPException(status_code=503, detail="NexusFS not initialized")
        content, mtime_ms = await read_written_bytes(nexus_fs, path, op_context)
        text = decode_index_text(content)
        if text is None:
            raise _skipped(REASON_NON_TEXT)
        result = await search_daemon.index_documents(
            [build_document(path, text, mtime_ms=mtime_ms)], zone_id=index_zone
        )
        verdict = verdict_for_path(result, path)
        if verdict.status == STATUS_SKIPPED:
            raise _skipped(verdict.reason or REASON_EMPTY)
        return {
            "status": "indexed",
            "path": path,
            "change_type": change_type,
            "index_seq": verdict.index_seq,
        }

    return await run_zone_scoped(_get_zone_registry(request), target_zone, _work)
