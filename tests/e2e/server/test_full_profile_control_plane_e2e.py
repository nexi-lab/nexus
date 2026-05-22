"""Real full-profile control-plane RPC smoke and latency coverage."""

from __future__ import annotations

import statistics
import time
import uuid
from typing import Any

import grpc
import pytest

pytestmark = [pytest.mark.e2e]

ADMIN_KEY = "test-e2e-api-key-12345"


def _call_rpc(
    stub: Any,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    api_key: str = ADMIN_KEY,
    timeout: float = 20.0,
) -> tuple[float, dict[str, Any]]:
    from nexus.grpc.vfs import vfs_pb2
    from nexus.lib.rpc_codec import decode_rpc_message, encode_rpc_message

    start = time.perf_counter()
    resp = stub.Call(
        vfs_pb2.CallRequest(
            method=method,
            payload=encode_rpc_message(params or {}),
            auth_token=api_key,
        ),
        timeout=timeout,
    )
    elapsed_ms = (time.perf_counter() - start) * 1000
    body = decode_rpc_message(resp.payload)
    assert not resp.is_error, f"{method} failed: {body}"
    assert isinstance(body, dict)
    return elapsed_ms, body


def test_full_profile_control_plane_rpc_correctness_and_latency(nexus_server) -> None:
    from nexus.grpc.vfs import vfs_pb2_grpc

    target = f"127.0.0.1:{nexus_server['port'] + 2}"
    channel = grpc.insecure_channel(target)
    grpc.channel_ready_future(channel).result(timeout=20)
    stub = vfs_pb2_grpc.NexusVFSServiceStub(channel)
    timings: dict[str, float] = {}

    try:
        suffix = uuid.uuid4().hex[:8]

        for method, params in [
            ("admin_write_permission", {"tuples": []}),
            (
                "admin_create_key",
                {
                    "name": f"e2e-user-key-{suffix}",
                    "zone_id": "root",
                    "user_id": f"e2e-user-{suffix}",
                    "subject_type": "user",
                    "is_admin": False,
                },
            ),
            ("admin_list_keys", {"limit": 10}),
            ("audit_list", {"limit": 5}),
            ("audit_export", {"fmt": "json"}),
            ("events_replay", {"limit": 5}),
            ("governance_alerts", {"limit": 5}),
            ("governance_rings", {}),
            ("governance_status", {}),
            ("pay_balance", {"agent_id": f"e2e-agent-{suffix}"}),
            ("pay_history", {"limit": 5}),
            (
                "pay_transfer",
                {
                    "from_agent": f"e2e-agent-{suffix}",
                    "to": f"e2e-agent-dst-{suffix}",
                    "amount": "1.25",
                    "memo": "full-profile e2e",
                },
            ),
            ("federation_list_zones", {}),
            ("federation_cluster_info", {"zone_id": "root"}),
        ]:
            elapsed, body = _call_rpc(stub, method, params)
            timings[method] = elapsed
            assert "result" in body

        key_id = _call_rpc(
            stub,
            "admin_create_key",
            {
                "name": f"e2e-update-key-{suffix}",
                "zone_id": "root",
                "user_id": f"e2e-update-user-{suffix}",
                "subject_type": "user",
                "is_admin": False,
            },
        )[1]["result"]["key_id"]

        for method, params in [
            ("admin_get_key", {"key_id": key_id}),
            ("admin_update_key", {"key_id": key_id, "name": f"e2e-renamed-{suffix}"}),
            ("admin_revoke_key", {"key_id": key_id}),
        ]:
            elapsed, body = _call_rpc(stub, method, params)
            timings[method] = elapsed
            assert "result" in body

        provisioned_user = f"e2e-provision-{suffix}"
        elapsed, body = _call_rpc(
            stub,
            "provision_user",
            {
                "user_id": provisioned_user,
                "email": f"{provisioned_user}@example.com",
                "zone_id": f"e2e-zone-{suffix}",
                "create_api_key": False,
                "create_agents": False,
            },
        )
        timings["provision_user"] = elapsed
        assert body["result"]["user_id"] == provisioned_user

        elapsed, body = _call_rpc(
            stub,
            "deprovision_user",
            {
                "user_id": provisioned_user,
                "zone_id": f"e2e-zone-{suffix}",
                "delete_user_record": True,
                "force": True,
            },
        )
        timings["deprovision_user"] = elapsed
        assert body["result"]["user_id"] == provisioned_user

    finally:
        channel.close()

    p50 = statistics.median(timings.values())
    p95 = sorted(timings.values())[min(len(timings) - 1, int(len(timings) * 0.95))]
    print(
        "\n  full-profile control-plane gRPC: "
        f"methods={len(timings)} p50={p50:.1f}ms p95={p95:.1f}ms "
        f"max={max(timings.values()):.1f}ms"
    )
    print(
        "  per-method: "
        + ", ".join(f"{method}={elapsed:.1f}ms" for method, elapsed in sorted(timings.items()))
    )
    assert p50 < 250
    assert max(timings.values()) < 2_500
