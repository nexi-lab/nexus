"""Workspace and Search clients for the owning Kernel."""

from __future__ import annotations

import asyncio
import builtins
import time
from typing import TYPE_CHECKING, Any, cast

from nexus.contracts.protocols.activity import EventKind, Result, emit
from nexus.lib.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext
    from nexus.core.nexus_fs import NexusFS


class SearchService:
    """Translate workspace and Search calls through their owning filesystem."""

    def __init__(
        self,
        kernel: Any,
        default_context: OperationContext | None = None,
        nexus_fs: NexusFS | None = None,
    ) -> None:
        self._kernel = kernel
        self._default_context = default_context
        self._nexus_fs = nexus_fs

    def _get_routing_params(self, context: Any) -> tuple[str | None, str | None, bool]:
        """Extract zone_id, agent_id, is_admin from context."""
        if self._nexus_fs:
            return self._nexus_fs._get_context_identity(context)
        return None, None, False

    def _record_read_if_tracking(
        self,
        context: Any,
        resource_type: str,
        resource_id: str,
        access_type: str = "content",
    ) -> None:
        """Record read for dependency tracking (Issue #1166)."""
        if context and getattr(context, "track_reads", False):
            kernel = getattr(self._nexus_fs, "_kernel", None) if self._nexus_fs else None
            zone_id = getattr(context, "zone_id", None) or "root"
            revision = kernel.get_zone_revision(zone_id) if kernel else 0
            context.record_read(resource_type, resource_id, revision, access_type)

    @rpc_expose(description="List files in directory")
    def list(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        show_parsed: bool = True,
        context: Any = None,
        limit: int | None = None,
        cursor: str | None = None,
        all_zones: bool = False,
    ) -> Any:
        """List through the filesystem's directory enumeration contract."""
        if self._nexus_fs is None:
            raise RuntimeError("Workspace listing requires a filesystem client")
        context = context or self._default_context
        result = self._nexus_fs.sys_readdir(
            path,
            recursive=recursive,
            details=details,
            show_parsed=show_parsed,
            context=context,
            limit=limit,
            cursor=cursor,
            all_zones=all_zones,
        )
        self._record_read_if_tracking(context, "directory", path, "list")
        return result

    @rpc_expose(description="Find files matching a glob pattern")
    def glob(
        self,
        pattern: str,
        path: str = "/",
        context: Any = None,  # noqa: ARG002
        files: builtins.list[str] | None = None,
    ) -> builtins.list[str]:
        """Discover files through the owning Kernel's SearchService."""
        response = self._kernel.call_rpc("glob", {"pattern": pattern, "path": path, "files": files})
        return cast(builtins.list[str], response["matches"])

    @rpc_expose(description="Search file contents")
    async def grep(
        self,
        pattern: str,
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 100,
        search_mode: str = "auto",
        context: Any = None,
        before_context: int = 0,
        after_context: int = 0,
        invert_match: bool = False,
        files: builtins.list[str] | None = None,
        block_type: str | None = None,
        section: str | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """Public grep entry point with activity-event instrumentation (#3791).

        Wraps :meth:`_grep_impl` to record SEARCH events on success and
        BLOCKED events on exceptions.  Wall-clock latency is captured.
        ``zone_id`` is derived from the operation context when available;
        ``token_hash`` is not currently extractable from the search
        ``context`` object so it is left as ``None``.
        """
        _start = time.monotonic()
        _zone: str | None = None
        try:
            _zone, _, _ = self._get_routing_params(context)
        except Exception:  # pragma: no cover - never fail emit setup
            _zone = None
        try:
            result = await self._grep_impl(
                pattern=pattern,
                path=path,
                file_pattern=file_pattern,
                ignore_case=ignore_case,
                max_results=max_results,
                search_mode=search_mode,
                context=context,
                before_context=before_context,
                after_context=after_context,
                invert_match=invert_match,
                files=files,
                block_type=block_type,
                section=section,
            )
        except Exception:
            emit(
                kind=EventKind.SEARCH,
                result=Result.BLOCKED,
                actor_token_hash=None,
                subject_zone=_zone,
                latency_ms=int((time.monotonic() - _start) * 1000),
            )
            raise
        emit(
            kind=EventKind.SEARCH,
            result=Result.OK,
            actor_token_hash=None,
            subject_zone=_zone,
            subject_extra={"hits": len(result) if hasattr(result, "__len__") else None},
            latency_ms=int((time.monotonic() - _start) * 1000),
        )
        return result

    async def _grep_impl(
        self,
        pattern: str,
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 100,
        search_mode: str = "auto",
        context: Any = None,  # noqa: ARG002
        before_context: int = 0,
        after_context: int = 0,
        invert_match: bool = False,
        files: builtins.list[str] | None = None,
        block_type: str | None = None,
        section: str | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """Forward discovery; filtering reads the current bytes in the Rust plugin."""
        response = await asyncio.to_thread(
            self._kernel.call_rpc,
            "grep",
            {
                "pattern": pattern,
                "path": path,
                "file_pattern": file_pattern,
                "ignore_case": ignore_case,
                "max_results": max_results,
                "search_mode": search_mode,
                "before_context": before_context,
                "after_context": after_context,
                "invert_match": invert_match,
                "files": files,
                "block_type": block_type,
                "section": section,
            },
        )
        return cast(builtins.list[dict[str, Any]], response["results"])

    @rpc_expose(description="Search documents using natural language queries")
    async def semantic_search(
        self,
        query: str,
        path: str = "/",
        limit: int = 10,
        filters: dict[str, Any] | None = None,
        search_mode: str = "semantic",
        context: "OperationContext | None" = None,
    ) -> builtins.list[dict[str, Any]]:
        """Public semantic-search entry point with activity-event instrumentation (#3791).

        Wraps :meth:`_semantic_search_impl` to record SEARCH events on
        success and BLOCKED events on exceptions. Mirrors the ``grep``
        wrapper pattern so all primary SearchService surfaces feed the
        same activity stream.
        """
        _start = time.monotonic()
        _zone: str | None = None
        try:
            _zone, _, _ = self._get_routing_params(context)
        except Exception:  # pragma: no cover - never fail emit setup
            _zone = None
        try:
            result = await self._semantic_search_impl(
                query=query,
                path=path,
                limit=limit,
                filters=filters,
                search_mode=search_mode,
                context=context,
            )
        except Exception:
            emit(
                kind=EventKind.SEARCH,
                result=Result.BLOCKED,
                actor_token_hash=None,
                subject_zone=_zone,
                subject_extra={"mode": search_mode},
                latency_ms=int((time.monotonic() - _start) * 1000),
            )
            raise
        emit(
            kind=EventKind.SEARCH,
            result=Result.OK,
            actor_token_hash=None,
            subject_zone=_zone,
            subject_extra={
                "mode": search_mode,
                "hits": len(result) if hasattr(result, "__len__") else None,
            },
            latency_ms=int((time.monotonic() - _start) * 1000),
        )
        return result

    async def _semantic_search_impl(
        self,
        query: str,
        path: str = "/",
        limit: int = 10,
        filters: dict[str, Any] | None = None,
        search_mode: str = "semantic",
        context: "OperationContext | None" = None,
    ) -> builtins.list[dict[str, Any]]:
        """Query the owning kernel; credentials and permissions are resolved by its host."""
        response = await asyncio.to_thread(
            self._kernel.call_rpc,
            "semantic_search",
            {
                "query": query,
                "path": path,
                "limit": limit,
                "filters": filters,
                "search_mode": search_mode,
                "zone_id": getattr(context, "zone_id", None),
            },
        )
        return cast(builtins.list[dict[str, Any]], response["results"])

    @rpc_expose(description="Index files through the search plugin")
    async def semantic_search_index(
        self,
        path: str = "/",
        recursive: bool = True,
        max_docs: int = 10_000,
        context: "OperationContext | None" = None,
    ) -> dict[str, int]:
        """Index current VFS bytes and return the host's indexed and skipped counts."""
        return cast(
            dict[str, int],
            await asyncio.to_thread(
                self._kernel.call_rpc,
                "semantic_search_index",
                {
                    "path": path,
                    "recursive": recursive,
                    "max_docs": max_docs,
                    "zone_id": getattr(context, "zone_id", None),
                },
            ),
        )

    @rpc_expose(description="Get semantic search indexing statistics")
    async def semantic_search_stats(self) -> dict[str, Any]:
        """Read index statistics through the owning kernel's SearchService."""
        return cast(
            dict[str, Any], await asyncio.to_thread(self._kernel.call_rpc, "semantic_search_stats")
        )
