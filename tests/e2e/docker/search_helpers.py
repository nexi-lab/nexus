"""gRPC wrappers for `nexus.search.v1.SearchService`.

Mirrors the shape of the vfs_* wrappers in `runbook_helpers.py` — typed
requests returned as `{"result": {...}}` on success, `{"error": {...}}`
on the plugin's structured `error` field being set.

Lazy imports keep collection-time cost zero even when the search proto
stubs are not present (the runner image adds them; a locally-run test
without the image would surface a clean ImportError at first call site
rather than a mysterious collection failure).
"""

from __future__ import annotations

from tests.e2e.docker.runbook_helpers import (
    ADMIN_API_KEY,
    GRPC_CHANNEL_OPTIONS,
)


def _open_search_stub(target: str):
    """Open a SearchServiceStub against `target`, returning (channel, stub).

    Caller owns the channel lifecycle.  Lazy imports keep collection-time
    cost zero — see the module docstring.
    """
    import grpc

    from nexus.grpc.search.v1 import search_pb2_grpc

    channel = grpc.insecure_channel(target, options=GRPC_CHANNEL_OPTIONS)
    return channel, search_pb2_grpc.SearchServiceStub(channel)


def search_glob(
    target: str,
    root_path: str,
    pattern: str,
    *,
    max_results: int = 0,
    sort_recency: bool = False,
    api_key: str = ADMIN_API_KEY,
    timeout: float = 30,
) -> dict:
    """Typed Glob RPC.

    Returns ``{"result": {"paths": [...], "truncated": bool}}`` on
    success or ``{"error": str}`` when the plugin sets the response's
    optional `error` field.  ``max_results=0`` uses the plugin's
    server-side default (10_000 for glob per the proto contract).
    """
    from nexus.grpc.search.v1 import search_pb2

    channel, stub = _open_search_stub(target)
    try:
        req = search_pb2.GlobRequest(
            root_path=root_path,
            pattern=pattern,
            max_results=max_results,
            auth_token=api_key,
            sort_recency=sort_recency,
        )
        resp = stub.Glob(req, timeout=timeout)
        if resp.HasField("error"):
            return {"error": resp.error}
        return {
            "result": {
                "paths": list(resp.paths),
                "truncated": resp.truncated,
            }
        }
    finally:
        channel.close()


def search_index(
    target: str,
    root_path: str,
    *,
    zone_id: str = "",
    recursive: bool = True,
    max_docs: int = 0,
    api_key: str = ADMIN_API_KEY,
    timeout: float = 120,
) -> dict:
    """Typed Index RPC (P1 keyword-only).

    Returns ``{"result": {"indexed_count": int, "skipped_count": int}}``
    on success or ``{"error": str}`` when the plugin sets the response's
    optional `error` field.  ``max_docs=0`` uses the plugin's server-side
    default (10_000 per the proto contract).
    """
    from nexus.grpc.search.v1 import search_pb2

    channel, stub = _open_search_stub(target)
    try:
        req = search_pb2.IndexRequest(
            root_path=root_path,
            zone_id=zone_id,
            recursive=recursive,
            max_docs=max_docs,
            auth_token=api_key,
        )
        resp = stub.Index(req, timeout=timeout)
        if resp.HasField("error"):
            return {"error": resp.error}
        return {
            "result": {
                "indexed_count": resp.indexed_count,
                "skipped_count": resp.skipped_count,
            }
        }
    finally:
        channel.close()


def search_query(
    target: str,
    q: str,
    *,
    zone_id: str = "",
    limit: int = 0,
    path_filter: str = "",
    query_type: str = "keyword",
    api_key: str = ADMIN_API_KEY,
    timeout: float = 30,
) -> dict:
    """Typed Query RPC (P1 keyword-only).

    Returns ``{"result": {"results": [...]}}`` on success or
    ``{"error": str}``.  Each result is
    ``{"path": str, "chunk_index": int, "chunk_text": str,
    "score": float, "zone_id": str, "mtime_ms": int | None}``.

    ``query_type`` accepts the string forms mirrored from the
    Python router: ``"keyword"``, ``"semantic"``, ``"hybrid"``.
    P1 rejects the latter two with an error message referencing
    the phase that will lift the gate.  ``limit=0`` uses the
    plugin's server-side default (10 per the proto contract).
    """
    from nexus.grpc.search.v1 import search_pb2

    qt_map = {
        "": search_pb2.QUERY_TYPE_UNSPECIFIED,
        "keyword": search_pb2.QUERY_TYPE_KEYWORD,
        "semantic": search_pb2.QUERY_TYPE_SEMANTIC,
        "hybrid": search_pb2.QUERY_TYPE_HYBRID,
    }
    if query_type not in qt_map:
        raise ValueError(f"unknown query_type: {query_type!r}")

    channel, stub = _open_search_stub(target)
    try:
        req = search_pb2.QueryRequest(
            q=q,
            zone_id=zone_id,
            limit=limit,
            path_filter=path_filter,
            query_type=qt_map[query_type],
            auth_token=api_key,
        )
        resp = stub.Query(req, timeout=timeout)
        if resp.HasField("error"):
            return {"error": resp.error}
        return {
            "result": {
                "results": [
                    {
                        "path": r.path,
                        "chunk_index": r.chunk_index,
                        "chunk_text": r.chunk_text,
                        "score": r.score,
                        "zone_id": r.zone_id,
                        "mtime_ms": r.mtime_ms if r.HasField("mtime_ms") else None,
                    }
                    for r in resp.results
                ]
            }
        }
    finally:
        channel.close()


def search_grep(
    target: str,
    root_path: str,
    pattern: str,
    *,
    file_pattern: str = "",
    ignore_case: bool = False,
    max_results: int = 0,
    before_context: int = 0,
    after_context: int = 0,
    invert_match: bool = False,
    sort_recency: bool = False,
    api_key: str = ADMIN_API_KEY,
    timeout: float = 60,
) -> dict:
    """Typed Grep RPC.

    Returns ``{"result": {"matches": [...], "truncated": bool}}`` where
    each match is ``{"path": str, "line_number": int, "line": str,
    "before": [str], "after": [str]}``.  ``max_results=0`` uses the
    plugin's server-side default (1_000 for grep per the proto contract).
    """
    from nexus.grpc.search.v1 import search_pb2

    channel, stub = _open_search_stub(target)
    try:
        req = search_pb2.GrepRequest(
            root_path=root_path,
            pattern=pattern,
            file_pattern=file_pattern,
            ignore_case=ignore_case,
            max_results=max_results,
            before_context=before_context,
            after_context=after_context,
            invert_match=invert_match,
            auth_token=api_key,
            sort_recency=sort_recency,
        )
        resp = stub.Grep(req, timeout=timeout)
        if resp.HasField("error"):
            return {"error": resp.error}
        return {
            "result": {
                "matches": [
                    {
                        "path": m.path,
                        "line_number": m.line_number,
                        "line": m.line,
                        "before": list(m.before),
                        "after": list(m.after),
                    }
                    for m in resp.matches
                ],
                "truncated": resp.truncated,
            }
        }
    finally:
        channel.close()
