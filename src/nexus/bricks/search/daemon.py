"""Async Search management client for the owning Kernel’s Rust plugin."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import grpc

from nexus.grpc.search.v1 import search_pb2, search_pb2_grpc
from nexus.lib.request_credentials import request_api_key
from nexus.remote.search_response import search_stats

T = TypeVar("T")

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


class _CallerCredentialsInterceptor(grpc.aio.UnaryUnaryClientInterceptor):
    """Attach the current caller to every RPC on the shared search channel."""

    async def intercept_unary_unary(
        self, continuation: Any, client_call_details: Any, request: Any
    ) -> Any:
        credential = request_api_key.get()
        if credential is not None:
            metadata = tuple(client_call_details.metadata or ()) + (
                ("authorization", f"Bearer {credential}"),
            )
            client_call_details = client_call_details._replace(metadata=metadata)
        return await continuation(client_call_details, request)


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
        return grpc.aio.secure_channel(
            target, creds, interceptors=[_CallerCredentialsInterceptor()]
        )
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
    return grpc.aio.insecure_channel(target, interceptors=[_CallerCredentialsInterceptor()])


class SearchDaemon:
    """Async client for the Rust search plugin.

    One lazy channel serves callers from any event loop. Calls and shutdown
    run on the channel's owning loop while preserving request credentials.
    """

    def __init__(self, target: str | None = None) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_lock = threading.Lock()
        self._target = target or os.environ.get(_TARGET_ENV, _DEFAULT_TARGET)
        self._channel: grpc.aio.Channel | None = None
        self._stub: search_pb2_grpc.SearchServiceStub | None = None
        self._initialized = True

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    async def startup(self) -> None:
        """No-op — connection opens lazily on first RPC."""
        return

    async def shutdown(self) -> None:
        if self._loop is None:
            return

        async def close() -> None:
            if self._channel is not None:
                await self._channel.close()
                self._channel = None
                self._stub = None

        await self._on_channel_loop(close)

    async def _on_channel_loop(self, work: Callable[[], Awaitable[T]]) -> T:
        """Keep the shared channel on its owner loop, including during shutdown."""
        current = asyncio.get_running_loop()
        if self._loop is None:
            with self._loop_lock:
                if self._loop is None:
                    self._loop = current
        owner = self._loop
        if owner is current:
            return await work()
        if not owner.is_running():
            raise RuntimeError("Search channel owner loop is not running")

        async def invoke() -> T:
            return await work()

        # Threadsafe submission preserves the caller's ContextVars. Cancellation
        # of wrap_future also cancels the submitted RPC on the channel loop.
        invocation = invoke()
        try:
            submitted = asyncio.run_coroutine_threadsafe(invocation, owner)
        except RuntimeError:
            invocation.close()
            raise
        return await asyncio.wrap_future(submitted)

    async def _rpc(self, method: str, request: Any) -> Any:
        async def invoke() -> Any:
            return await getattr(self._get_stub(), method)(request)

        return await self._on_channel_loop(invoke)

    def _get_stub(self) -> search_pb2_grpc.SearchServiceStub:
        """Construct the channel lazily on its owning event loop."""
        if self._stub is None:
            self._channel = _build_channel(self._target)
            self._stub = search_pb2_grpc.SearchServiceStub(self._channel)
        return self._stub

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
        resp = await self._rpc("IndexDocuments", req)
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
            # #4736: WHICH paths were content-skipped, so write+index
            # can answer per document, and the plugin-wide sequence
            # stamped after the commit that made this batch visible
            # (``/search/stats`` ``last_index_seq >= index_seq`` ⇒
            # served).
            "skipped_paths": list(resp.skipped_paths),
            "index_seq": int(resp.index_seq),
        }

    async def notify_file_change(
        self,
        path: str,
        change_type: str = "update",
        *,
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        """Relay a per-file change event to the plugin.

        Returns the plugin's verdict: ``status`` is ``"accepted"`` when
        the index was MUTATED (``delete``) and ``"skipped"`` when the
        event was a pure ack.  Post-P12 ``create`` / ``update`` are
        ALWAYS ``"skipped"`` — no text travels on this RPC, so making a
        written file searchable takes ``index_documents`` (#4736).
        """
        req = search_pb2.NotifyFileChangeRequest(
            path=path,
            change_type=change_type,
            zone_id=zone_id or "",
        )
        resp = await self._rpc("NotifyFileChange", req)
        # Fail closed (review R4): a delete whose tombstone could not
        # be persisted must not report success — swallowing the error
        # here would leave orphaned vectors behind an HTTP 200.
        if resp.HasField("error"):
            raise RuntimeError(f"plugin notify_file_change failed: {resp.error}")
        return {"status": resp.status, "index_seq": int(resp.index_seq)}

    # ── Health + stats ────────────────────────────────────────

    async def get_health(self) -> dict[str, Any]:
        resp = await self._rpc("Health", search_pb2.HealthRequest())
        return {
            "status": resp.status,
            "detail": resp.detail,
            # #4725 structured writer liveness — twins of what ``detail``
            # narrates, so pollers gate on numbers.  ``fts_writer_faults``
            # > 0: a zone's writer faulted and has not yet proven itself
            # with a searchable commit (status at most "degraded").
            # ``fts_writer_unavailable`` > 0: writes to that zone fail
            # loudly (status "unavailable").  ``dispatch_panics`` counts
            # handler panics the plugin caught at its dispatch boundary
            # since process start — informational, each already failed
            # its own RPC.
            "fts_writer_faults": resp.fts_writer_faults,
            "fts_writer_unavailable": resp.fts_writer_unavailable,
            "last_verified_commit_age_ms": (
                resp.last_verified_commit_age_ms
                if resp.HasField("last_verified_commit_age_ms")
                else None
            ),
            "dispatch_panics": resp.dispatch_panics,
        }

    async def get_stats(self, zone_id: str | None = None) -> dict[str, Any]:
        """Read counters for the requested zone through the SearchService protocol."""
        response = await self._rpc("Stats", search_pb2.StatsRequest(zone_id=zone_id or ""))
        if response.error:
            raise RuntimeError(f"Search plugin statistics failed: {response.error}")
        return search_stats(response)
