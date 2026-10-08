"""Discovery RPC adapter on the filesystem's existing gRPC channel."""

from __future__ import annotations

from typing import Any, cast

import grpc

from nexus.grpc.search.v1 import search_pb2, search_pb2_grpc


class SearchClient:
    """Translate Python discovery calls; the host owns validation and access checks."""

    def __init__(self, channel: grpc.Channel) -> None:
        self._stub = search_pb2_grpc.SearchServiceStub(channel)

    def call(
        self,
        method: str,
        params: dict[str, Any],
        *,
        credential: str | None,
        timeout: float,
    ) -> dict[str, Any]:
        args = dict(params)
        root = args.pop("path", "/")
        pattern = args.pop("pattern")
        files = args.pop("files", None)
        expected_filters = 0
        request: search_pb2.GlobRequest | search_pb2.GrepRequest
        if method == "glob":
            request = search_pb2.GlobRequest(
                root_path=root,
                pattern=pattern,
                max_results=args.pop("max_results", 10_000),
                sort_recency=True,
            )
            rpc = self._stub.Glob
        elif method == "grep":
            mode = args.pop("search_mode", "auto")
            if mode not in ("auto", "raw"):
                raise ValueError(
                    "grep searches current file bytes; search_mode must be auto or raw"
                )
            request = search_pb2.GrepRequest(
                root_path=root,
                pattern=pattern,
                file_pattern=args.pop("file_pattern", None) or "",
                ignore_case=args.pop("ignore_case", False),
                max_results=args.pop("max_results", 100),
                before_context=args.pop("before_context", 0),
                after_context=args.pop("after_context", 0),
                invert_match=args.pop("invert_match", False),
                sort_recency=True,
            )
            for field, bit in (
                ("block_type", search_pb2.DISCOVERY_FILTER_BLOCK_TYPE),
                ("section", search_pb2.DISCOVERY_FILTER_SECTION),
            ):
                value = args.pop(field, None)
                if value is not None:
                    setattr(request, field, value)
                    expected_filters |= bit
            rpc = self._stub.Grep
        else:
            raise ValueError(f"Unknown discovery method: {method}")
        if args:
            raise TypeError(f"Unsupported {method} arguments: {', '.join(sorted(args))}")
        if files is not None:
            if not isinstance(files, list) or any(not isinstance(path, str) for path in files):
                raise ValueError("files must be a list of VFS paths")
            request.files.CopyFrom(search_pb2.DiscoveryFiles(paths=files))
            expected_filters |= search_pb2.DISCOVERY_FILTER_FILES

        # Presence matters: an explicit empty credential must not fall back to mTLS.
        metadata = (("authorization", f"Bearer {credential}"),) if credential is not None else ()
        response = rpc(request, metadata=metadata, timeout=timeout)
        if response.applied_filters & expected_filters != expected_filters:
            raise RuntimeError("Search host did not apply every requested discovery filter")
        if response.HasField("error"):
            raise RuntimeError(f"Search discovery failed: {response.error}")
        if method == "glob":
            return {"matches": list(response.paths), "truncated": response.truncated}
        grep_request = cast(search_pb2.GrepRequest, request)
        results = []
        for hit in response.matches:
            entry: dict[str, Any] = {"file": hit.path, "line": hit.line_number, "content": hit.line}
            if grep_request.before_context:
                entry["before_context"] = [
                    {"line": hit.line_number - len(hit.before) + index, "content": line}
                    for index, line in enumerate(hit.before)
                ]
            if grep_request.after_context:
                entry["after_context"] = [
                    {"line": hit.line_number + index + 1, "content": line}
                    for index, line in enumerate(hit.after)
                ]
            if hit.HasField("section"):
                entry["section"] = {
                    "heading": hit.section.heading,
                    "depth": hit.section.depth,
                    "line_start": hit.section.line_start,
                    "line_end": hit.section.line_end,
                }
            results.append(entry)
        return {"results": results, "truncated": response.truncated}
