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
