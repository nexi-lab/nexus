"""Issue #4137 real E2E coverage for agent/workspace/snapshot/version APIs."""

from __future__ import annotations

import base64
import json
import time
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx

API_KEY = "test-e2e-api-key-12345"
HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "X-Nexus-Subject": "user:admin",
    "X-Nexus-Zone-Id": "root",
}
E2E_TIMEOUT_SECONDS = 30.0


def _b64(text: str) -> dict[str, str]:
    return {"__type__": "bytes", "data": base64.b64encode(text.encode()).decode()}


def _time_ms(perf: dict[str, float], name: str, fn: Callable[[], Any]) -> Any:
    start = time.perf_counter()
    try:
        return fn()
    finally:
        perf[name] = round((time.perf_counter() - start) * 1000, 2)


def _rpc(
    client: httpx.Client,
    method: str,
    params: dict[str, Any] | None,
    perf: dict[str, float],
) -> tuple[int, dict[str, Any]]:
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": 1}

    def _call() -> httpx.Response:
        return client.post(
            f"/api/nfs/{method}",
            json=body,
            headers=HEADERS,
            timeout=E2E_TIMEOUT_SECONDS,
        )

    resp = _time_ms(perf, f"rpc:{method}", _call)
    return resp.status_code, resp.json()


def _rpc_result(
    client: httpx.Client,
    method: str,
    params: dict[str, Any] | None,
    perf: dict[str, float],
) -> Any:
    status, payload = _rpc(client, method, params, perf)
    assert status == 200, f"{method} returned HTTP {status}: {payload}"
    assert payload.get("error") in (None, {}), f"{method} returned RPC error: {payload}"
    return payload.get("result")


def _rpc_error(
    client: httpx.Client,
    method: str,
    params: dict[str, Any] | None,
    perf: dict[str, float],
) -> dict[str, Any]:
    status, payload = _rpc(client, method, params, perf)
    assert status == 200, f"{method} returned HTTP {status}: {payload}"
    assert payload.get("error"), f"{method} unexpectedly succeeded: {payload}"
    return payload["error"]


def _http(
    client: httpx.Client,
    perf: dict[str, float],
    method: str,
    path: str,
    **kwargs: Any,
) -> httpx.Response:
    kwargs.setdefault("headers", HEADERS)
    kwargs.setdefault("timeout", E2E_TIMEOUT_SECONDS)
    return _time_ms(
        perf,
        f"http:{method.upper()} {path}",
        lambda: client.request(method, path, **kwargs),
    )


def _write_file(client: httpx.Client, path: str, text: str, perf: dict[str, float]) -> Any:
    return _rpc_result(client, "write", {"path": path, "content": _b64(text)}, perf)


def test_issue_4137_lifecycle_surface_real_e2e(test_app: httpx.Client) -> None:
    """Drive #4137 runtime APIs through the live daemon HTTP surface."""
    client = test_app
    perf: dict[str, float] = {}
    suffix = uuid.uuid4().hex[:8]

    # Version APIs.
    version_path = f"/zone/root/user/admin/workspace/issue-4137-{suffix}/versions.txt"
    _write_file(client, version_path, "line one\n", perf)
    _write_file(client, version_path, "line two\n", perf)
    versions = _rpc_result(client, "list_versions", {"path": version_path}, perf)
    assert isinstance(versions, list)
    assert len(versions) >= 2
    assert _rpc_result(client, "get_version", {"path": version_path, "version": 1}, perf)
    diff = _rpc_result(
        client,
        "diff_versions",
        {"path": version_path, "v1": 1, "v2": 2, "mode": "metadata"},
        perf,
    )
    assert isinstance(diff, dict)
    _rpc_result(client, "rollback", {"path": version_path, "version": 1}, perf)

    # Agent lifecycle APIs, including stale-generation failure.
    agent_id = f"admin,issue-4137-{suffix}"
    agent = _rpc_result(
        client,
        "register_agent",
        {
            "agent_id": agent_id,
            "name": "Issue 4137 Agent",
            "description": "Live E2E agent",
            "metadata": {"issue": "4137"},
        },
        perf,
    )
    assert agent["agent_id"] == agent_id
    updated_agent = _rpc_result(
        client,
        "update_agent",
        {
            "agent_id": agent_id,
            "name": "Issue 4137 Agent Updated",
            "metadata": {"updated": True},
        },
        perf,
    )
    assert updated_agent["name"] == "Issue 4137 Agent Updated"
    assert any(a["agent_id"] == agent_id for a in _rpc_result(client, "list_agents", {}, perf))
    assert _rpc_result(client, "get_agent", {"agent_id": agent_id}, perf)["agent_id"] == agent_id
    assert _rpc_result(client, "agent_heartbeat", {"agent_id": agent_id}, perf) == {"ok": True}
    by_zone = _rpc_result(client, "agent_list_by_zone", {"zone_id": "root"}, perf)
    assert any(a["agent_id"] == agent_id for a in by_zone)
    transitioned = _rpc_result(
        client,
        "agent_transition",
        {
            "agent_id": agent_id,
            "target_state": "CONNECTED",
            "expected_generation": agent["generation"],
        },
        perf,
    )
    assert transitioned["agent_id"] == agent_id
    assert transitioned["state"] == "ready"
    stale_error = _rpc_error(
        client,
        "agent_transition",
        {
            "agent_id": agent_id,
            "target_state": "CONNECTED",
            "expected_generation": agent["generation"],
        },
        perf,
    )
    assert "stale generation" in stale_error["message"].lower()

    # Workspace RPC APIs, including missing-workspace failure.
    workspace_path = f"/zone/root/user/admin/workspace/issue-4137-{suffix}"
    loaded_path = f"/zone/root/user/admin/workspace/issue-4137-load-{suffix}"
    workspace = _rpc_result(
        client,
        "register_workspace",
        {
            "path": workspace_path,
            "name": "Issue 4137 Workspace",
            "description": "Live E2E workspace",
            "metadata": {"issue": "4137"},
        },
        perf,
    )
    assert workspace["path"] == workspace_path
    assert (
        _rpc_result(
            client,
            "update_workspace",
            {"path": workspace_path, "name": "Issue 4137 Workspace Updated"},
            perf,
        )["name"]
        == "Issue 4137 Workspace Updated"
    )
    assert _rpc_result(client, "get_workspace_info", {"path": workspace_path}, perf)["path"] == (
        workspace_path
    )
    assert any(
        w["path"] == workspace_path for w in _rpc_result(client, "list_workspaces", {}, perf)
    )
    load_result = _rpc_result(
        client,
        "load_workspace_config",
        {"workspaces": [{"path": loaded_path, "name": "Loaded Issue 4137"}]},
        perf,
    )
    assert load_result["workspaces_registered"] == 1
    missing_workspace = _rpc_error(
        client,
        "workspace_snapshot",
        {"workspace_path": f"/zone/root/user/admin/workspace/missing-{suffix}"},
        perf,
    )
    assert "workspace not registered" in missing_workspace["message"].lower()

    _write_file(client, f"{workspace_path}/a.txt", "a\n", perf)
    snap1 = _rpc_result(
        client,
        "workspace_snapshot",
        {"workspace_path": workspace_path, "description": "before"},
        perf,
    )
    _write_file(client, f"{workspace_path}/b.txt", "b\n", perf)
    snap2 = _rpc_result(
        client,
        "workspace_snapshot",
        {"workspace_path": workspace_path, "description": "after"},
        perf,
    )
    log = _rpc_result(client, "workspace_log", {"workspace_path": workspace_path}, perf)
    assert len(log) >= 2
    ws_diff = _rpc_result(
        client,
        "workspace_diff",
        {
            "workspace_path": workspace_path,
            "snapshot_1": snap1["snapshot_number"],
            "snapshot_2": snap2["snapshot_number"],
        },
        perf,
    )
    assert isinstance(ws_diff, dict)
    restore = _rpc_result(
        client,
        "workspace_restore",
        {"workspace_path": workspace_path, "snapshot_number": snap1["snapshot_number"]},
        perf,
    )
    assert "files_deleted" in restore

    # Transactional snapshot RPC APIs.
    txn = _rpc_result(
        client,
        "snapshot_create",
        {"description": "issue 4137 rpc txn", "ttl_seconds": 60},
        perf,
    )
    txn_id = txn["transaction_id"]
    assert txn_id
    assert (
        _rpc_result(client, "snapshot_get", {"transaction_id": txn_id}, perf)["transaction_id"]
        == txn_id
    )
    assert (
        _rpc_result(client, "snapshot_list_entries", {"transaction_id": txn_id}, perf)["count"] == 0
    )
    assert _rpc_result(client, "snapshot_list", {}, perf)["count"] >= 1
    assert _rpc_result(client, "snapshot_commit", {"transaction_id": txn_id}, perf)["status"] in {
        "committed",
        "COMMITTED",
    }
    rollback_txn = _rpc_result(
        client,
        "snapshot_create",
        {"description": "issue 4137 rpc rollback txn", "ttl_seconds": 60},
        perf,
    )
    assert _rpc_result(
        client, "snapshot_restore", {"txn_id": rollback_txn["transaction_id"]}, perf
    )["status"] in {"rolled_back", "ROLLED_BACK"}

    # Transactional snapshot REST APIs.
    rest_txn_resp = _http(
        client,
        perf,
        "POST",
        "/api/v2/snapshots",
        json={"description": "issue 4137 rest txn", "ttl_seconds": 60},
    )
    assert rest_txn_resp.status_code == 201, rest_txn_resp.text
    rest_txn_id = rest_txn_resp.json()["transaction_id"]
    list_resp = _http(client, perf, "GET", "/api/v2/snapshots")
    assert list_resp.status_code == 200, list_resp.text
    get_resp = _http(client, perf, "GET", f"/api/v2/snapshots/{rest_txn_id}")
    assert get_resp.status_code == 200, get_resp.text
    entries_resp = _http(client, perf, "GET", f"/api/v2/snapshots/{rest_txn_id}/entries")
    assert entries_resp.status_code == 200, entries_resp.text
    commit_resp = _http(client, perf, "POST", f"/api/v2/snapshots/{rest_txn_id}/commit")
    assert commit_resp.status_code == 200, commit_resp.text
    rest_rollback_resp = _http(
        client,
        perf,
        "POST",
        "/api/v2/snapshots",
        json={"description": "issue 4137 rest rollback txn", "ttl_seconds": 60},
    )
    rest_rollback_id = rest_rollback_resp.json()["transaction_id"]
    rollback_resp = _http(client, perf, "POST", f"/api/v2/snapshots/{rest_rollback_id}/rollback")
    assert rollback_resp.status_code == 200, rollback_resp.text

    # Workspace registry REST APIs.
    rest_workspace_path = f"/zone/root/user/admin/workspace/issue-4137-rest-{suffix}"
    rest_list = _http(client, perf, "GET", "/api/v2/registry/workspaces")
    assert rest_list.status_code == 200, rest_list.text
    rest_create = _http(
        client,
        perf,
        "POST",
        "/api/v2/registry/workspaces",
        json={
            "path": rest_workspace_path,
            "name": "Issue 4137 REST Workspace",
            "metadata": {"issue": "4137"},
        },
    )
    assert rest_create.status_code == 201, rest_create.text
    rest_workspace_url = f"/api/v2/registry/workspaces/{quote(rest_workspace_path.lstrip('/'))}"
    rest_get = _http(client, perf, "GET", rest_workspace_url)
    assert rest_get.status_code == 200, rest_get.text
    rest_patch = _http(
        client,
        perf,
        "PATCH",
        rest_workspace_url,
        json={"name": "Issue 4137 REST Workspace Updated"},
    )
    assert rest_patch.status_code == 200, rest_patch.text
    rest_delete = _http(client, perf, "DELETE", rest_workspace_url)
    assert rest_delete.status_code == 200, rest_delete.text

    assert _rpc_result(client, "unregister_workspace", {"path": loaded_path}, perf) is True
    assert _rpc_result(client, "unregister_workspace", {"path": workspace_path}, perf) is True
    assert _rpc_result(client, "delete_agent", {"agent_id": agent_id}, perf) is True

    print("ISSUE_4137_E2E_PERF " + json.dumps(perf, sort_keys=True))
