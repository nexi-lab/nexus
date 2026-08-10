"""Python-side proxy for the Rust ``nexus-search-plugin`` cdylib.

Every method the FastAPI search router touches on
``app.state.search_daemon`` lives here as a thin gRPC wrapper.  The
plugin owns the FTS + ANN indices, embeddings, and the parked-event
queue; this file exists so Python callers keep the same
``search_daemon.<method>`` shape they always saw.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

import grpc

from nexus.bricks.search.results import BaseSearchResult
from nexus.grpc.search.v1 import search_pb2, search_pb2_grpc

if TYPE_CHECKING:
    from nexus.contracts.search_types import SearchRequest

logger = logging.getLogger(__name__)

# gRPC dial target env vars — the boot layer sets these based on the
# same NEXUS_CLUSTER_GRPC / SEARCH_PLUGIN_TARGET convention the
# docker E2E already uses.
_TARGET_ENV = "NEXUS_SEARCH_PLUGIN_TARGET"
_DEFAULT_TARGET = "127.0.0.1:2126"

_QUERY_TYPE_MAP = {
    "keyword": search_pb2.QUERY_TYPE_KEYWORD,
    "semantic": search_pb2.QUERY_TYPE_SEMANTIC,
    "hybrid": search_pb2.QUERY_TYPE_HYBRID,
}
_FUSION_METHOD_MAP = {
    "rrf": search_pb2.FUSION_METHOD_RRF,
    "weighted": search_pb2.FUSION_METHOD_WEIGHTED,
    "rrf_weighted": search_pb2.FUSION_METHOD_RRF_WEIGHTED,
}


class SearchDaemon:
    """Rust-plugin-backed search daemon.  Same SearchBrickProtocol
    surface as :class:`SearchDaemon`; methods forward to the plugin
    via gRPC.

    Cheap to construct — the gRPC channel is opened lazily on first
    call so a lite deployment that never search-queries pays nothing.
    """

    def __init__(self, target: str | None = None) -> None:
        self._target = target or os.environ.get(_TARGET_ENV, _DEFAULT_TARGET)
        self._channel: grpc.aio.Channel | None = None
        self._stub: search_pb2_grpc.SearchServiceStub | None = None
        self._initialized = True  # matches Python daemon's boot posture

    # ── SearchBrickProtocol boilerplate ────────────────────────

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    async def startup(self) -> None:
        """No-op — connection opens lazily on first RPC."""
        return

    async def shutdown(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None

    def _get_stub(self) -> search_pb2_grpc.SearchServiceStub:
        """Lazy channel + stub construction.  aio channel so the
        Python daemon's async methods stay async without a thread hop."""
        if self._stub is None:
            self._channel = grpc.aio.insecure_channel(self._target)
            self._stub = search_pb2_grpc.SearchServiceStub(self._channel)
        return self._stub

    # ── Core search ────────────────────────────────────────────

    async def search(self, request: "SearchRequest") -> list[BaseSearchResult]:
        """Forward a SearchRequest to the Rust plugin's Query RPC."""
        req = search_pb2.QueryRequest(
            q=request.query,
            zone_id=request.zone_id or "",
            limit=request.limit,
            path_filter=request.path_filter or "",
            query_type=_QUERY_TYPE_MAP.get(request.search_type, search_pb2.QUERY_TYPE_UNSPECIFIED),
            alpha=request.alpha,
            fusion_method=_FUSION_METHOD_MAP.get(
                request.fusion_method, search_pb2.FUSION_METHOD_UNSPECIFIED
            ),
            rrf_k=request.rrf_k,
            expand=request.expand or "",
            recency_mode=request.recency or "",
            recency_weight=request.recency_weight or 0.0,
            recency_half_life_days=request.recency_half_life_days or 0.0,
            path_prefix_boosts=request.path_prefix_boosts or {},
        )
        resp = await self._get_stub().Query(req)
        if resp.HasField("error"):
            logger.warning("rust search returned error: %s", resp.error)
            return []
        return [_result_to_base(r) for r in resp.results]

    async def batch_search(
        self,
        requests: list["SearchRequest"],
    ) -> list[list[BaseSearchResult]]:
        """Forward a batch of queries to the Rust plugin."""
        pb_queries = [
            search_pb2.QueryRequest(
                q=r.query,
                zone_id=r.zone_id or "",
                limit=r.limit,
                path_filter=r.path_filter or "",
                query_type=_QUERY_TYPE_MAP.get(r.search_type, search_pb2.QUERY_TYPE_UNSPECIFIED),
                path_prefix_boosts=r.path_prefix_boosts or {},
            )
            for r in requests
        ]
        req = search_pb2.BatchQueryRequest(queries=pb_queries)
        resp = await self._get_stub().BatchQuery(req)
        return [[_result_to_base(hit) for hit in sub.results] for sub in resp.responses]

    async def locate(
        self,
        path: str,
        *,
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        req = search_pb2.LocateRequest(path=path, zone_id=zone_id or "")
        resp = await self._get_stub().Locate(req)
        return {
            "indexed": resp.indexed,
            "chunk_count": resp.chunk_count,
            "mtime_ms": resp.mtime_ms if resp.HasField("mtime_ms") else None,
            "zone_id": resp.zone_id,
        }

    # ── Indexing ───────────────────────────────────────────────

    async def index_documents(
        self,
        documents: list[dict[str, Any]],
        *,
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        pb_docs = []
        for d in documents:
            entry = search_pb2.DocumentInput(
                path=d.get("path", ""),
                text=d.get("text", ""),
                zone_id=d.get("zone_id", ""),
            )
            if "mtime_ms" in d and d["mtime_ms"] is not None:
                entry.mtime_ms = int(d["mtime_ms"])
            pb_docs.append(entry)
        req = search_pb2.IndexDocumentsRequest(documents=pb_docs, zone_id=zone_id or "")
        resp = await self._get_stub().IndexDocuments(req)
        return {
            "indexed": resp.indexed_count,
            "skipped": list(resp.parked_paths),
        }

    async def notify_file_change(
        self,
        path: str,
        change_type: str = "update",
        *,
        zone_id: str | None = None,
    ) -> None:
        req = search_pb2.NotifyFileChangeRequest(
            path=path,
            change_type=change_type,
            zone_id=zone_id or "",
        )
        await self._get_stub().NotifyFileChange(req)

    # ── Indexed-directories registry ──────────────────────────

    async def add_indexed_directory(
        self,
        zone_id: str,
        directory_path: str,
    ) -> dict[str, bool]:
        req = search_pb2.AddIndexedDirectoryRequest(path=directory_path, zone_id=zone_id)
        resp = await self._get_stub().AddIndexedDirectory(req)
        return {"added": resp.added}

    async def remove_indexed_directory(
        self,
        zone_id: str,
        directory_path: str,
    ) -> str:
        req = search_pb2.RemoveIndexedDirectoryRequest(path=directory_path, zone_id=zone_id)
        resp = await self._get_stub().RemoveIndexedDirectory(req)
        return "removed" if resp.removed else "not_found"

    async def list_indexed_directories(self, zone_id: str) -> list[str]:
        req = search_pb2.ListIndexedDirectoriesRequest(zone_id=zone_id)
        resp = await self._get_stub().ListIndexedDirectories(req)
        return [d.path for d in resp.directories]

    # ── Zone indexing modes ────────────────────────────────────

    async def set_zone_indexing_mode(self, zone_id: str, mode: str) -> Any:
        req = search_pb2.SetZoneIndexingModeRequest(zone_id=zone_id, mode=mode)
        resp = await self._get_stub().SetZoneIndexingMode(req)
        if resp.HasField("error"):
            raise ValueError(resp.error)
        return {"zone_id": zone_id, "mode": mode}

    async def get_zone_indexing_modes(self) -> dict[str, str]:
        """Snapshot per-zone indexing modes from the plugin."""
        req = search_pb2.ListZoneIndexingModesRequest()
        resp = await self._get_stub().ListZoneIndexingModes(req)
        return {m.zone_id: m.mode for m in resp.modes}

    # ── Health + stats ────────────────────────────────────────

    async def get_health(self) -> dict[str, Any]:
        resp = await self._get_stub().Health(search_pb2.HealthRequest())
        return {"status": resp.status, "detail": resp.detail}

    async def get_stats(self) -> dict[str, Any]:
        resp = await self._get_stub().Stats(search_pb2.StatsRequest())
        return {
            "fts_doc_count": resp.fts_doc_count,
            "fts_path_count": resp.fts_path_count,
            "ann_chunk_count": resp.ann_chunk_count,
            "parked_count": resp.parked_count,
        }


def _result_to_base(pb: search_pb2.QueryResult) -> BaseSearchResult:
    """Convert a proto QueryResult into the Python daemon's result
    shape.  Fields Python has that Rust doesn't (matched_field,
    reranker_score, etc.) stay None — the Rust plugin doesn't
    surface them yet."""
    return BaseSearchResult(
        path=pb.path,
        chunk_text=pb.chunk_text,
        score=float(pb.score),
        chunk_index=int(pb.chunk_index),
        zone_id=pb.zone_id or None,
        macro_text=pb.expanded_context or None,
    )
