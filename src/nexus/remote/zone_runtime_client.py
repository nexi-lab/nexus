"""ZoneRuntimePort — the single typed boundary to nexus-vfs zone runtime (2C).

Everything the application layer knows about physical zones comes through
here: create/join/status/mount/unmount/deprovision receipts, capability
probes and timeouts. No service may talk to ZoneManager, raft internals or
the kernel's private structures directly (§6.5); conversely this port never
decides product policy — it executes with a trusted, authenticated context
and reports what physically happened.

The read-back rule lives here: ``receipt`` is what the runtime said, and only
a receipt (never "the RPC didn't error", never "the SQL row exists") turns a
zone active or a mount observed.
"""

# ruff: noqa: ARG002  # protocol-shaped parameters keep the interface

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeReceipt:
    """What the physical runtime reported for one operation."""

    ok: bool
    physical_identity: str | None = None
    membership: str | None = None
    runtime_revision: str | None = None
    capabilities: tuple[str, ...] = ()
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class ZoneRuntimeUnavailable(Exception):
    """The runtime could not be reached or answered unusably.

    Timeouts surface here — callers treat the operation as unknown and poll,
    never as failed-and-retry-with-a-new-idempotency-key.
    """


class ZoneRuntimePort(Protocol):
    """The typed surface every zone mutation goes through."""

    def create_zone(self, *, zone_id: str, ctx: dict[str, Any]) -> RuntimeReceipt: ...

    def join_zone(
        self, *, zone_id: str, peers: list[str], ctx: dict[str, Any]
    ) -> RuntimeReceipt: ...

    def zone_status(self, *, zone_id: str, ctx: dict[str, Any]) -> RuntimeReceipt: ...

    def mount(
        self, *, parent_zone_id: str, target_zone_id: str, path: str, ctx: dict[str, Any]
    ) -> RuntimeReceipt: ...

    def unmount(self, *, mount_ref: str, ctx: dict[str, Any]) -> RuntimeReceipt: ...

    def remove_replica(
        self, *, zone_id: str, force: bool, ctx: dict[str, Any]
    ) -> RuntimeReceipt: ...

    def deprovision(
        self, *, zone_id: str, deletion_epoch: int, ctx: dict[str, Any]
    ) -> RuntimeReceipt: ...

    def get_operation(self, *, operation_id: str, ctx: dict[str, Any]) -> RuntimeReceipt: ...

    def probe_capabilities(self, *, ctx: dict[str, Any]) -> tuple[str, ...]: ...


class KernelRpcZoneRuntimePort:
    """ZoneRuntimePort over the existing kernel RPC channel.

    The 763f8c0 typed zone-runtime surface is reached through the cluster
    service registry this process already holds (``kernel_client``-style
    channel). Method names mirror the zone_runtime service; payloads are
    plain JSON, and the trusted ``ctx`` (authenticated OperationContext
    fields) travels with every call — the runtime re-checks it, the product
    layer never trusts payload-supplied identity.
    """

    def __init__(self, call_channel: Any, *, timeout_s: float = 30.0) -> None:
        self._call = call_channel
        self._timeout_s = timeout_s

    def _call_raw(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            call = getattr(self._call, "zone_runtime_call", None)
            if call is None:
                raise TypeError("call channel does not expose typed zone_runtime_call")
            raw = call(method, payload, timeout_s=self._timeout_s)
        except TimeoutError as exc:
            raise ZoneRuntimeUnavailable(f"{method} timed out") from exc
        except Exception as exc:  # transport-level failure: unknown, not failed
            raise ZoneRuntimeUnavailable(f"{method} unreachable: {exc}") from exc
        if not isinstance(raw, dict):
            raise ZoneRuntimeUnavailable(f"{method} returned an unusable response")
        return raw

    def _invoke(self, method: str, payload: dict[str, Any]) -> RuntimeReceipt:
        raw = self._call_raw(method, payload)
        return receipt_from_raw(raw)

    @staticmethod
    def _operation_id(ctx: dict[str, Any]) -> str:
        operation_id = ctx.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            raise ZoneRuntimeUnavailable("trusted context is missing operation_id")
        return operation_id

    def create_zone(self, *, zone_id: str, ctx: dict[str, Any]) -> RuntimeReceipt:
        return self._invoke(
            "ZoneCreate",
            {
                "operation_id": self._operation_id(ctx),
                "request_hash": 0,
                "zone_id": zone_id,
                "peers": list(ctx.get("peers") or ()),
            },
        )

    def join_zone(self, *, zone_id: str, peers: list[str], ctx: dict[str, Any]) -> RuntimeReceipt:
        return self._invoke(
            "ZoneJoin",
            {
                "operation_id": self._operation_id(ctx),
                "request_hash": 0,
                "zone_id": zone_id,
                "peers": peers,
                "learner": bool(ctx.get("learner", False)),
            },
        )

    def zone_status(self, *, zone_id: str, ctx: dict[str, Any]) -> RuntimeReceipt:
        raw = self._call_raw("ZoneStatus", {"zone_id": zone_id})
        presence = str(raw.get("presence") or "PRESENCE_UNKNOWN")
        cluster = raw.get("cluster") if isinstance(raw.get("cluster"), dict) else {}
        ok = presence in {"RESIDENT", "HOSTED_NOT_RESIDENT"}
        revision = None
        if cluster:
            revision = f"{cluster.get('term', 0)}:{cluster.get('commit_index', 0)}:{cluster.get('applied_index', 0)}"
        return RuntimeReceipt(
            ok=ok,
            physical_identity=zone_id if ok else None,
            membership=presence,
            runtime_revision=revision,
            error=None if ok else presence,
            raw=raw,
        )

    def mount(
        self,
        *,
        parent_zone_id: str,
        target_zone_id: str,
        path: str,
        ctx: dict[str, Any],
    ) -> RuntimeReceipt:
        return self._invoke(
            "ZoneMount",
            {
                "operation_id": self._operation_id(ctx),
                "request_hash": 0,
                "parent_zone_id": parent_zone_id,
                "target_zone_id": target_zone_id,
                "mount_path": path,
            },
        )

    def unmount(self, *, mount_ref: str, ctx: dict[str, Any]) -> RuntimeReceipt:
        parent_zone_id = str(ctx.get("parent_zone_id") or "")
        path = str(ctx.get("path") or "")
        if not parent_zone_id or not path:
            raise ZoneRuntimeUnavailable("unmount context is missing parent_zone_id/path")
        return self._invoke(
            "ZoneUnmount",
            {
                "operation_id": self._operation_id(ctx),
                "request_hash": 0,
                "parent_zone_id": parent_zone_id,
                "mount_path": path,
                "mount_ref": mount_ref,
            },
        )

    def remove_replica(self, *, zone_id: str, force: bool, ctx: dict[str, Any]) -> RuntimeReceipt:
        return self._invoke(
            "ZoneRemoveReplica",
            {
                "operation_id": self._operation_id(ctx),
                "request_hash": 0,
                "zone_id": zone_id,
                "force": force,
            },
        )

    def deprovision(
        self, *, zone_id: str, deletion_epoch: int, ctx: dict[str, Any]
    ) -> RuntimeReceipt:
        return self._invoke(
            "ZoneDeprovision",
            {
                "operation_id": self._operation_id(ctx),
                "request_hash": 0,
                "zone_id": zone_id,
                "deletion_epoch": deletion_epoch,
            },
        )

    def get_operation(self, *, operation_id: str, ctx: dict[str, Any]) -> RuntimeReceipt:
        raw = self._call_raw("GetZoneOperation", {"operation_id": operation_id})
        status = str(raw.get("status") or "").upper()
        if status == "COMPLETED" and isinstance(raw.get("receipt"), dict):
            receipt = receipt_from_raw(raw["receipt"])
            return RuntimeReceipt(
                ok=receipt.ok,
                physical_identity=receipt.physical_identity,
                membership=receipt.membership,
                runtime_revision=receipt.runtime_revision,
                capabilities=receipt.capabilities,
                error=receipt.error,
                raw={**receipt.raw, "journal": raw},
            )
        if status == "FAILED":
            return RuntimeReceipt(
                ok=False, error=str(raw.get("error") or "runtime failed"), raw=raw
            )
        raise ZoneRuntimeUnavailable(f"operation {operation_id} is {status or 'UNKNOWN'}")

    def probe_capabilities(self, *, ctx: dict[str, Any]) -> tuple[str, ...]:
        raw = self._call_raw("GetRuntimeCapabilities", {})
        auth: dict[str, Any] = raw["auth"] if isinstance(raw.get("auth"), dict) else {}
        if not (raw.get("data_plane_ready") and auth.get("armed")):
            return ()
        capabilities = raw.get("capabilities")
        if not isinstance(capabilities, list):
            return ()
        return tuple(str(capability) for capability in capabilities)


def receipt_from_raw(raw: Any) -> RuntimeReceipt:
    """Normalize a runtime answer into a receipt; absence of an error is not
    success — only an explicit ok/identity makes one."""
    if not isinstance(raw, dict):
        return RuntimeReceipt(ok=False, error=f"unusable runtime answer: {raw!r}")
    outcome = str(raw.get("outcome") or "")
    cluster = raw.get("cluster") if isinstance(raw.get("cluster"), dict) else {}
    successful = {
        "CREATED",
        "JOINED",
        "ALREADY_PRESENT",
        "MOUNTED",
        "UNMOUNTED",
        "REPLICA_REMOVED",
        "DEPROVISIONED",
    }
    runtime_revision = raw.get("runtime_revision")
    if runtime_revision is None and cluster:
        runtime_revision = (
            f"{cluster.get('term', 0)}:{cluster.get('commit_index', 0)}:"
            f"{cluster.get('applied_index', 0)}"
        )
    return RuntimeReceipt(
        ok=bool(raw.get("ok")) or outcome in successful,
        physical_identity=raw.get("physical_identity")
        or (raw.get("zone_id") if outcome in successful else None),
        membership=raw.get("membership") or outcome or None,
        runtime_revision=runtime_revision,
        capabilities=tuple(raw.get("capabilities") or ()),
        error=raw.get("error"),
        raw=dict(raw),
    )


class NullZoneRuntimePort:
    """Port used when assembly has not armed a runtime (tests, worker-off).

    Mutations report unavailable — fail-closed — instead of pretending: a
    build without the runtime capability must never mark anything active.
    """

    def create_zone(self, *, zone_id: str, ctx: dict[str, Any]) -> RuntimeReceipt:
        return RuntimeReceipt(ok=False, error="zone runtime not armed")

    def join_zone(self, *, zone_id: str, peers: list[str], ctx: dict[str, Any]) -> RuntimeReceipt:
        return RuntimeReceipt(ok=False, error="zone runtime not armed")

    def zone_status(self, *, zone_id: str, ctx: dict[str, Any]) -> RuntimeReceipt:
        return RuntimeReceipt(ok=False, error="zone runtime not armed")

    def mount(
        self, *, parent_zone_id: str, target_zone_id: str, path: str, ctx: dict[str, Any]
    ) -> RuntimeReceipt:
        return RuntimeReceipt(ok=False, error="zone runtime not armed")

    def unmount(self, *, mount_ref: str, ctx: dict[str, Any]) -> RuntimeReceipt:
        return RuntimeReceipt(ok=False, error="zone runtime not armed")

    def remove_replica(self, *, zone_id: str, force: bool, ctx: dict[str, Any]) -> RuntimeReceipt:
        return RuntimeReceipt(ok=False, error="zone runtime not armed")

    def deprovision(
        self, *, zone_id: str, deletion_epoch: int, ctx: dict[str, Any]
    ) -> RuntimeReceipt:
        return RuntimeReceipt(ok=False, error="zone runtime not armed")

    def get_operation(self, *, operation_id: str, ctx: dict[str, Any]) -> RuntimeReceipt:
        raise ZoneRuntimeUnavailable("zone runtime not armed")

    def probe_capabilities(self, *, ctx: dict[str, Any]) -> tuple[str, ...]:
        return ()
