from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.grpc.vfs import zone_runtime_pb2
from nexus.remote.rpc_transport import RPCTransport
from nexus.remote.zone_runtime_client import (
    KernelRpcZoneRuntimePort,
    ZoneRuntimeUnavailable,
    receipt_from_raw,
)


class Channel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, float]] = []

    def zone_runtime_call(self, method: str, payload: dict, *, timeout_s: float):
        self.calls.append((method, payload, timeout_s))
        if method == "GetRuntimeCapabilities":
            return {
                "data_plane_ready": True,
                "auth": {"armed": True},
                "permission": {"provider_armed": True},
                "capabilities": ["zone-runtime:create", "zone-runtime:status"],
            }
        if method == "GetZoneOperation":
            return {
                "operation_id": payload["operation_id"],
                "status": "COMPLETED",
                "receipt": {
                    "operation_id": payload["operation_id"],
                    "zone_id": "team-alpha",
                    "outcome": "CREATED",
                },
            }
        if method == "ZoneStatus":
            return {
                "zone_id": payload["zone_id"],
                "presence": "RESIDENT",
                "cluster": {"term": "2", "commit_index": "8", "applied_index": "8"},
            }
        return {"zone_id": payload.get("zone_id"), "outcome": "CREATED"}


def test_typed_create_carries_stable_operation_id() -> None:
    channel = Channel()
    runtime = KernelRpcZoneRuntimePort(channel, timeout_s=7)
    receipt = runtime.create_zone(zone_id="team-alpha", ctx={"operation_id": "op-1"})
    assert receipt.ok and receipt.physical_identity == "team-alpha"
    assert channel.calls == [
        (
            "ZoneCreate",
            {
                "operation_id": "op-1",
                "request_hash": 0,
                "zone_id": "team-alpha",
                "peers": [],
            },
            7,
        )
    ]


def test_status_and_capability_readback_are_fail_closed() -> None:
    channel = Channel()
    runtime = KernelRpcZoneRuntimePort(channel)
    status = runtime.zone_status(zone_id="team-alpha", ctx={})
    assert status.ok and status.membership == "RESIDENT"
    assert status.runtime_revision == "2:8:8"
    assert runtime.probe_capabilities(ctx={}) == (
        "zone-runtime:create",
        "zone-runtime:status",
    )


def test_runtime_probe_does_not_duplicate_the_product_rebac_gate() -> None:
    class PreInstallSnapshotChannel(Channel):
        def zone_runtime_call(self, method: str, payload: dict, *, timeout_s: float):
            raw = super().zone_runtime_call(method, payload, timeout_s=timeout_s)
            if method == "GetRuntimeCapabilities":
                raw["permission"] = {}
            return raw

    runtime = KernelRpcZoneRuntimePort(PreInstallSnapshotChannel())
    assert runtime.probe_capabilities(ctx={}) == (
        "zone-runtime:create",
        "zone-runtime:status",
    )


def test_operation_journal_recovers_the_original_receipt() -> None:
    runtime = KernelRpcZoneRuntimePort(Channel())
    receipt = runtime.get_operation(operation_id="op-1", ctx={})
    assert receipt.ok
    assert receipt.physical_identity == "team-alpha"


def test_pending_operation_remains_unknown_and_retryable() -> None:
    class PendingChannel(Channel):
        def zone_runtime_call(self, method: str, payload: dict, *, timeout_s: float):
            if method == "GetZoneOperation":
                return {"operation_id": payload["operation_id"], "status": "PENDING"}
            return super().zone_runtime_call(method, payload, timeout_s=timeout_s)

    runtime = KernelRpcZoneRuntimePort(PendingChannel())
    with pytest.raises(ZoneRuntimeUnavailable, match="PENDING"):
        runtime.get_operation(operation_id="op-1", ctx={})


def test_receipt_needs_an_explicit_success_outcome() -> None:
    assert not receipt_from_raw({"zone_id": "team-alpha"}).ok


def test_rpc_transport_builds_the_typed_mutation_dto() -> None:
    captured = {}

    def create(request, *, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return zone_runtime_pb2.ZoneReceipt(
            operation_id=request.mutation.operation_id,
            zone_id=request.zone_id,
            kind="create",
            outcome="CREATED",
        )

    transport = object.__new__(RPCTransport)
    transport._auth_token = "runtime-token"
    transport._timeout = 30.0
    transport._zone_runtime_stub = SimpleNamespace(ZoneCreate=create)
    raw = transport.zone_runtime_call(
        "ZoneCreate",
        {"operation_id": "op-typed", "zone_id": "team-alpha", "peers": ["n2:2126"]},
        timeout_s=4,
    )

    request = captured["request"]
    assert request.auth_token == "runtime-token"
    assert request.mutation.operation_id == "op-typed"
    assert list(request.peers) == ["n2:2126"]
    assert captured["timeout"] == 4
    assert raw["outcome"] == "CREATED"
