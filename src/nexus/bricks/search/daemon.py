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
from nexus.contracts.search_types import BatchQueryFailure
from nexus.grpc.search.v1 import search_pb2, search_pb2_grpc

if TYPE_CHECKING:
    from nexus.contracts.search_types import SearchRequest

logger = logging.getLogger(__name__)

# gRPC dial target env vars — the boot layer sets these based on the
# same NEXUS_CLUSTER_GRPC / SEARCH_PLUGIN_TARGET convention the
# docker E2E already uses.
_TARGET_ENV = "NEXUS_SEARCH_PLUGIN_TARGET"
_DEFAULT_TARGET = "127.0.0.1:2126"

# Transport security (#4622 review R1).  Plaintext is only acceptable
# on the loopback topology the default target implies; a cross-host
# deployment must either enable TLS or explicitly accept the risk.
#
# - NEXUS_SEARCH_PLUGIN_TLS=true          → TLS channel
# - NEXUS_SEARCH_PLUGIN_TLS_CA=<path>     → CA bundle for server verification
#                                           (system roots when unset)
# - NEXUS_SEARCH_PLUGIN_TLS_CERT/_KEY     → client cert+key pair for mTLS
# - NEXUS_SEARCH_PLUGIN_ALLOW_INSECURE=true
#     → permit PLAINTEXT to a non-loopback target (trusted-network
#       escape hatch; dev compose sets it for host.docker.internal).
_TLS_ENV = "NEXUS_SEARCH_PLUGIN_TLS"
_TLS_CA_ENV = "NEXUS_SEARCH_PLUGIN_TLS_CA"
_TLS_CERT_ENV = "NEXUS_SEARCH_PLUGIN_TLS_CERT"
_TLS_KEY_ENV = "NEXUS_SEARCH_PLUGIN_TLS_KEY"
_ALLOW_INSECURE_ENV = "NEXUS_SEARCH_PLUGIN_ALLOW_INSECURE"

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")

# Docker's host-gateway alias resolves to the machine RUNNING the
# container (Docker Desktop / OrbStack) — a container→host link on one
# box, not a network hop.  Treated as same-machine so the dev default
# target works out of the box WITHOUT a blanket ALLOW_INSECURE that
# would also waive protection for genuinely remote targets (review
# R5): an operator pointing NEXUS_SEARCH_PLUGIN_TARGET at another host
# still hits the plaintext refusal.
_SAME_MACHINE_HOSTS = _LOOPBACK_HOSTS + ("host.docker.internal",)


def _target_is_loopback(target: str) -> bool:
    """True when ``target``'s host part is same-machine (loopback or
    Docker's host-gateway alias)."""
    host = target.rsplit(":", 1)[0] if ":" in target else target
    return host in _SAME_MACHINE_HOSTS


def _read_env_file(env_name: str) -> bytes | None:
    path = os.environ.get(env_name)
    if not path:
        return None
    with open(path, "rb") as f:
        return f.read()


def _build_channel(target: str) -> "grpc.aio.Channel":
    """Construct the plugin channel per the transport-security env contract.

    TLS on ⇒ ``secure_channel`` with server verification (CA bundle or
    system roots) and optional mTLS client identity.  TLS off ⇒
    plaintext, but ONLY to loopback unless the operator explicitly set
    ``NEXUS_SEARCH_PLUGIN_ALLOW_INSECURE=true`` — a raised error here
    surfaces through the boot probe's fail-soft path as a loud
    "search disabled" warning instead of silently shipping tenant
    queries over an unauthenticated cross-host link.
    """
    if os.environ.get(_TLS_ENV, "").lower() in ("true", "1", "yes"):
        cert = _read_env_file(_TLS_CERT_ENV)
        key = _read_env_file(_TLS_KEY_ENV)
        creds = grpc.ssl_channel_credentials(
            root_certificates=_read_env_file(_TLS_CA_ENV),
            private_key=key,
            certificate_chain=cert,
        )
        return grpc.aio.secure_channel(target, creds)
    if not _target_is_loopback(target) and os.environ.get(_ALLOW_INSECURE_ENV, "").lower() not in (
        "true",
        "1",
        "yes",
    ):
        raise RuntimeError(
            f"refusing PLAINTEXT gRPC to non-loopback search plugin target {target!r}: "
            f"set {_TLS_ENV}=true (+ {_TLS_CA_ENV}/{_TLS_CERT_ENV}/{_TLS_KEY_ENV}) for a "
            f"secured host, or {_ALLOW_INSECURE_ENV}=true to accept an unauthenticated "
            "link on a trusted network"
        )
    if not _target_is_loopback(target):
        logger.warning(
            "search plugin transport is PLAINTEXT to non-loopback target %s "
            "(%s=true) — anyone on the network path can read queries and "
            "index content; prefer %s=true",
            target,
            _ALLOW_INSECURE_ENV,
            _TLS_ENV,
        )
    return grpc.aio.insecure_channel(target)


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
        Python daemon's async methods stay async without a thread hop.
        Channel security follows the NEXUS_SEARCH_PLUGIN_TLS* env
        contract — see :func:`_build_channel`."""
        if self._stub is None:
            self._channel = _build_channel(self._target)
            self._stub = search_pb2_grpc.SearchServiceStub(self._channel)
        return self._stub

    # ── Core search ────────────────────────────────────────────

    async def search_with_error(
        self, request: "SearchRequest"
    ) -> tuple[list[BaseSearchResult], str | None]:
        """Query RPC with the plugin's per-query error PRESERVED.

        Review R4: collapsing a backend failure (embedder down,
        provider timeout, misconfiguration) into a bare ``[]`` makes a
        degraded dependency indistinguishable from "no matches".  The
        HTTP single-query route surfaces the returned error additively
        — same contract shape as the batch endpoint's per-entry
        failures (#4612).
        """
        resp = await self._get_stub().Query(_request_to_pb(request))
        if resp.HasField("error"):
            logger.warning("rust search returned error: %s", resp.error)
            return [], resp.error
        return [_result_to_base(r) for r in resp.results], None

    async def search(self, request: "SearchRequest") -> list[BaseSearchResult]:
        """Forward a SearchRequest to the Rust plugin's Query RPC.

        Degrades a per-query backend error to ``[]`` — the documented
        interactive posture for the federated / service-layer callers.
        Callers that must DISTINGUISH failure from empty use
        :meth:`search_with_error`.
        """
        results, _error = await self.search_with_error(request)
        return results

    async def batch_search(
        self,
        requests: list["SearchRequest"],
    ) -> list[list[BaseSearchResult] | BatchQueryFailure]:
        """Forward a batch of queries to the Rust plugin.

        Positional with ``requests``.  An inner query the plugin failed
        (embedder unavailable, per-query timeout, backend error) comes
        back as :class:`BatchQueryFailure` — NOT an empty list — so the
        batch endpoint can emit its per-entry additive ``error`` field
        and fail-closed consumers can tell "searched, no matches" from
        "search failed" (#4612).
        """
        req = search_pb2.BatchQueryRequest(queries=[_request_to_pb(r) for r in requests])
        resp = await self._get_stub().BatchQuery(req)
        out: list[list[BaseSearchResult] | BatchQueryFailure] = []
        for sub in resp.responses:
            if sub.HasField("error"):
                out.append(BatchQueryFailure(error=sub.error))
            else:
                out.append([_result_to_base(hit) for hit in sub.results])
        return out

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
                # Tenant boundary (review R2): when the caller passes an
                # authorized zone, it OVERRIDES any per-document zone —
                # the plugin treats a non-empty doc zone as a routing
                # override, so honoring caller-controlled values here
                # would let one tenant write into another's index.  The
                # HTTP route additionally 403s explicit mismatches.
                zone_id=zone_id or d.get("zone_id", ""),
            )
            if "mtime_ms" in d and d["mtime_ms"] is not None:
                entry.mtime_ms = int(d["mtime_ms"])
            pb_docs.append(entry)
        req = search_pb2.IndexDocumentsRequest(documents=pb_docs, zone_id=zone_id or "")
        resp = await self._get_stub().IndexDocuments(req)
        # Fail closed: a populated error means FTS/ANN persistence broke
        # mid-batch — swallowing it here would let the route 200 an
        # incomplete index (review R1).  The route maps the raised
        # exception to HTTP 500 so clients retry.
        if resp.HasField("error"):
            raise RuntimeError(f"plugin index_documents failed: {resp.error}")
        return {
            "indexed": resp.indexed_count,
            "skipped": list(resp.parked_paths),
            # Content-level skips (empty/whitespace docs, chunkless
            # docs) — distinct from the projection-wait ``skipped``
            # paths above, which drive the route's 409.
            "skipped_count": resp.skipped_count,
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
        resp = await self._get_stub().NotifyFileChange(req)
        # Fail closed (review R4): a delete whose tombstone could not
        # be persisted must not report success — swallowing the error
        # here would leave orphaned vectors behind an HTTP 200.
        if resp.HasField("error"):
            raise RuntimeError(f"plugin notify_file_change failed: {resp.error}")

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
            # #4617 identity fields — pre-P12 stats consumers key on
            # these.  ``embedding_model`` empty on the wire means
            # keyword-only mode; surface that as None, matching the
            # old daemon's "no embedding model configured" contract.
            "backend": resp.backend or "rust-plugin",
            "embedding_model": resp.embedding_model or None,
            # #4623: non-zero while explicit index ops are in flight —
            # "empty results" then mean "still building", not "no
            # matches".
            "indexing_in_progress": resp.indexing_in_progress,
        }


def _request_to_pb(request: "SearchRequest") -> search_pb2.QueryRequest:
    """Map a SearchRequest onto the plugin's QueryRequest proto.

    Shared by ``search`` and ``batch_search`` so the batch path can
    never silently drop a tuning knob the single path forwards.
    """
    return search_pb2.QueryRequest(
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
        # #4628: title-arm attribution — optional proto field, so
        # presence (not zero-ness) decides None.
        title_score=float(pb.title_score) if pb.HasField("title_score") else None,
    )
