"""Issue #4136 real E2E coverage for mounts, connectors, OAuth, and MCP APIs."""

from __future__ import annotations

import json
import os
import shutil
import sys
import textwrap
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

API_KEY = "test-e2e-api-key-12345"
E2E_TIMEOUT_SECONDS = 30.0


def _resolve_issue_4136_kernel_binary(repo_root: Path) -> str | None:
    configured = os.environ.get("NEXUS_KERNEL_BINARY")
    if configured:
        return configured

    local_binary = repo_root / "target" / "debug" / "nexusd-cluster"
    if local_binary.exists():
        return str(local_binary)

    for binary_name in ("nexusd-cluster", "nexus-cluster"):
        resolved = shutil.which(binary_name)
        if resolved:
            return resolved

    return None


@pytest.fixture
def issue_4136_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure external-control dependencies before the real server starts."""
    kernel_binary = _resolve_issue_4136_kernel_binary(Path(__file__).resolve().parents[3])
    if kernel_binary is None:
        pytest.skip(
            "nexusd-cluster binary not found; build it with "
            "`cargo build --manifest-path rust/profiles/cluster/Cargo.toml "
            "--bin nexusd-cluster` or put nexusd-cluster/nexus-cluster on PATH"
        )

    monkeypatch.setenv("NEXUS_PROFILE", "full")
    monkeypatch.setenv("NEXUS_KERNEL_BINARY", kernel_binary)
    monkeypatch.setenv("NEXUS_ACTIVITY_ENABLED", "0")
    monkeypatch.setenv("NEXUS_CACHE_WARMUP_DEPTH", "0")
    monkeypatch.setenv("NEXUS_CACHE_WARMUP_MAX_FILES", "0")
    monkeypatch.setenv("NEXUS_DISABLE_VECTOR_SEARCH", "1")
    monkeypatch.setenv("NEXUS_RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("NEXUS_SEARCH_DAEMON", "false")
    monkeypatch.setenv("NEXUS_OAUTH_GOOGLE_CLIENT_ID", "issue-4136-client-id")
    monkeypatch.setenv("NEXUS_OAUTH_GOOGLE_CLIENT_SECRET", "issue-4136-client-secret")
    monkeypatch.delenv("KLAVIS_API_KEY", raising=False)


def test_issue_4136_kernel_binary_prefers_path_when_local_debug_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("NEXUS_KERNEL_BINARY", raising=False)
    monkeypatch.setattr(
        "shutil.which",
        lambda name: f"/usr/local/bin/{name}" if name == "nexusd-cluster" else None,
    )

    assert _resolve_issue_4136_kernel_binary(tmp_path) == "/usr/local/bin/nexusd-cluster"


@pytest.fixture
def issue_4136_app(issue_4136_env: None, test_app: httpx.Client) -> httpx.Client:
    return test_app


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {API_KEY}",
        "X-Nexus-Subject": "user:admin",
        "X-Nexus-Zone-Id": "root",
    }


def _time_ms(perf: dict[str, float], name: str, fn: Callable[[], Any]) -> Any:
    start = time.perf_counter()
    try:
        return fn()
    finally:
        perf[name] = round((time.perf_counter() - start) * 1000, 2)


def _rpc(client: httpx.Client, method: str, params: dict[str, Any] | None, perf: dict[str, float]):
    body = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params or {},
        "id": 1,
    }

    def _call() -> httpx.Response:
        return client.post(
            f"/api/nfs/{method}",
            json=body,
            headers=_headers(),
            timeout=E2E_TIMEOUT_SECONDS,
        )

    resp = _time_ms(perf, method, _call)
    payload = resp.json()
    return resp.status_code, payload


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


def _write_stdio_mcp_server(tmp_path: Path) -> Path:
    server_path = tmp_path / "issue_4136_mcp_server.py"
    server_path.write_text(
        textwrap.dedent(
            """
            import json
            import sys

            TOOL = {
                "name": "issue_4136_echo",
                "description": "Echo text",
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            }


            def respond(request, result):
                sys.stdout.write(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request.get("id"),
                            "result": result,
                        },
                        separators=(",", ":"),
                    )
                    + "\\n"
                )
                sys.stdout.flush()


            for line in sys.stdin:
                if not line.strip():
                    continue
                request = json.loads(line)
                method = request.get("method")
                if method == "initialize":
                    params = request.get("params") or {}
                    respond(
                        request,
                        {
                            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                            "capabilities": {"tools": {"listChanged": False}},
                            "serverInfo": {
                                "name": "issue-4136-e2e",
                                "version": "1.0.0",
                            },
                        },
                    )
                elif method == "tools/list":
                    respond(request, {"tools": [TOOL]})
                elif method == "tools/call":
                    text = ((request.get("params") or {}).get("arguments") or {}).get("text", "")
                    respond(
                        request,
                        {
                            "content": [{"type": "text", "text": text}],
                            "isError": False,
                        },
                    )
                elif method == "ping":
                    respond(request, {})
            """
        ).strip()
        + "\n"
    )
    return server_path


def test_issue_4136_mount_connector_oauth_mcp_real_e2e(
    issue_4136_app: httpx.Client,
    tmp_path: Path,
) -> None:
    """Drive every #4136 API family through a real server process."""
    client = issue_4136_app
    perf: dict[str, float] = {}
    suffix = uuid.uuid4().hex[:8]
    connector_mount = f"/issue-4136-http-{suffix}"
    rpc_mount = f"/issue-4136-rpc-{suffix}"
    saved_mount = f"/issue-4136-saved-{suffix}"
    http_mcp_name = f"issue-4136-http-mcp-{suffix}"
    rpc_mcp_name = f"issue-4136-rpc-mcp-{suffix}"
    stdio_server = _write_stdio_mcp_server(tmp_path)

    def _http(method: str, path: str, **kwargs: Any) -> httpx.Response:
        kwargs.setdefault("headers", _headers())
        kwargs.setdefault("timeout", E2E_TIMEOUT_SECONDS)
        return _time_ms(
            perf, f"{method.upper()} {path}", lambda: client.request(method, path, **kwargs)
        )

    # Connector HTTP APIs.
    connectors = _http("GET", "/api/v2/connectors")
    assert connectors.status_code == 200, connectors.text
    connector_names = {c["name"] for c in connectors.json()["connectors"]}
    assert "path_local" in connector_names

    available = _http("GET", "/api/v2/connectors/available")
    assert available.status_code == 200, available.text
    assert isinstance(available.json(), list)

    auth_init = _http(
        "POST",
        "/api/v2/connectors/auth/init",
        json={"connector_name": "gmail_connector", "provider": "gmail"},
    )
    assert auth_init.status_code == 200, auth_init.text
    state_token = auth_init.json()["state_token"]
    assert "accounts.google.com" in auth_init.json()["auth_url"]

    auth_status = _http(
        "GET", "/api/v2/connectors/auth/status", params={"state_token": state_token}
    )
    assert auth_status.status_code == 200, auth_status.text
    assert auth_status.json()["status"] == "pending"

    mount_resp = _http(
        "POST",
        "/api/v2/connectors/mount",
        json={
            "connector_type": "path_local",
            "mount_point": connector_mount,
            "config": {"root_path": str(tmp_path / "connector-store")},
        },
    )
    assert mount_resp.status_code == 200, mount_resp.text
    assert mount_resp.json()["mounted"] is True
    mount_payload = mount_resp.json()

    mounts_resp = _http("GET", "/api/v2/connectors/mounts")
    assert mounts_resp.status_code == 200, mounts_resp.text
    assert any(m["mount_point"] == connector_mount for m in mounts_resp.json()), {
        "mount_response": mount_payload,
        "mounts_response": mounts_resp.json(),
    }

    unmount_resp = _http(
        "POST",
        "/api/v2/connectors/unmount",
        json={
            "connector_type": "path_local",
            "mount_point": connector_mount,
            "config": {},
        },
    )
    assert unmount_resp.status_code == 200, unmount_resp.text
    assert unmount_resp.json()["mounted"] is False

    # Mount RPC APIs.
    storage_connectors = _rpc_result(client, "list_connectors", {"category": "storage"}, perf)
    assert any(c["name"] == "path_local" for c in storage_connectors)

    added = _rpc_result(
        client,
        "add_mount",
        {
            "mount_point": rpc_mount,
            "backend_type": "path_local",
            "backend_config": {"root_path": str(tmp_path / "rpc-store")},
        },
        perf,
    )
    assert added == rpc_mount

    listed_mounts = _rpc_result(client, "list_mounts", {}, perf)
    assert any(m["mount_point"] == rpc_mount for m in listed_mounts)

    update = _rpc_result(
        client,
        "update_mount",
        {"mount_point": rpc_mount, "backend_config": {"fsync": False}},
        perf,
    )
    assert update == {"updated": False, "mount_point": rpc_mount, "changed_keys": []}

    reauth_error = _rpc_error(
        client,
        "reauth_mount",
        {"mount_point": rpc_mount, "provider": "gmail", "user_email": "admin@example.com"},
        perf,
    )
    assert "No Python backend available for reauth" in str(reauth_error)

    removed = _rpc_result(client, "remove_mount", {"mount_point": rpc_mount}, perf)
    assert removed["removed"] is True

    mount_id = _rpc_result(
        client,
        "save_mount",
        {
            "mount_point": saved_mount,
            "backend_type": "path_local",
            "backend_config": {"root_path": str(tmp_path / "saved-store")},
            "description": "Issue 4136 saved mount E2E",
        },
        perf,
    )
    assert mount_id

    loaded = _rpc_result(client, "load_mount", {"mount_point": saved_mount}, perf)
    assert loaded == saved_mount

    saved_listed = _rpc_result(client, "list_mounts", {}, perf)
    assert any(m["mount_point"] == saved_mount for m in saved_listed)

    saved_removed = _rpc_result(client, "remove_mount", {"mount_point": saved_mount}, perf)
    assert saved_removed["removed"] is True

    deleted = _rpc_result(client, "delete_saved_mount", {"mount_point": saved_mount}, perf)
    assert deleted is True

    # OAuth RPC APIs. Success paths are local-control only; external token exchange is
    # intentionally verified as a clean error without contacting a real provider.
    providers = _rpc_result(client, "oauth_list_providers", {}, perf)
    assert any(p["name"] == "gmail" for p in providers)

    auth_url = _rpc_result(
        client,
        "oauth_get_auth_url",
        {"provider": "gmail", "redirect_uri": "http://localhost:5173/oauth/callback"},
        perf,
    )
    assert "accounts.google.com" in auth_url["url"]

    credentials = _rpc_result(client, "oauth_list_credentials", {}, perf)
    assert isinstance(credentials, list)

    test_credential = _rpc_result(
        client,
        "oauth_test_credential",
        {"provider": "gmail", "user_email": "missing@example.com"},
        perf,
    )
    assert test_credential["valid"] is False

    revoke_error = _rpc_error(
        client,
        "oauth_revoke_credential",
        {"provider": "gmail", "user_email": "missing@example.com"},
        perf,
    )
    assert "OAuth credential store not configured" in str(revoke_error)

    exchange_error = _rpc_error(
        client,
        "oauth_exchange_code",
        {"provider": "not-a-provider", "code": "dummy-code", "user_email": "admin@example.com"},
        perf,
    )
    assert "not-a-provider" in str(exchange_error)

    # MCP RPC APIs.
    connect_error = _rpc_error(
        client,
        "mcp_connect",
        {"provider": "gmail", "redirect_url": "http://localhost:5173/oauth/callback"},
        perf,
    )
    assert "KLAVIS_API_KEY" in str(connect_error)

    rpc_mounted = _rpc_result(
        client,
        "mcp_mount",
        {
            "name": rpc_mcp_name,
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(stdio_server)],
            "description": "Issue 4136 RPC MCP mount",
        },
        perf,
    )
    assert rpc_mounted["mounted"] is True
    assert rpc_mounted["tool_count"] >= 1

    rpc_mcp_mounts = _rpc_result(client, "mcp_list_mounts", {}, perf)
    assert any(m["name"] == rpc_mcp_name for m in rpc_mcp_mounts)

    rpc_tools = _rpc_result(client, "mcp_list_tools", {"name": rpc_mcp_name}, perf)
    assert any(t["name"] == "issue_4136_echo" for t in rpc_tools)

    rpc_synced = _rpc_result(client, "mcp_sync", {"name": rpc_mcp_name}, perf)
    assert rpc_synced["tool_count"] >= 1

    rpc_unmounted = _rpc_result(client, "mcp_unmount", {"name": rpc_mcp_name}, perf)
    assert rpc_unmounted == {"success": True, "name": rpc_mcp_name}

    # MCP HTTP APIs.
    http_mounted = _http(
        "POST",
        "/api/v2/mcp/mounts",
        json={
            "name": http_mcp_name,
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(stdio_server)],
            "description": "Issue 4136 HTTP MCP mount",
        },
    )
    assert http_mounted.status_code == 201, http_mounted.text
    assert http_mounted.json()["mounted"] is True

    http_mcp_mounts = _http("GET", "/api/v2/mcp/mounts")
    assert http_mcp_mounts.status_code == 200, http_mcp_mounts.text
    assert any(m["name"] == http_mcp_name for m in http_mcp_mounts.json()["mounts"])

    http_unmounted = _http("DELETE", f"/api/v2/mcp/mounts/{http_mcp_name}")
    assert http_unmounted.status_code == 200, http_unmounted.text
    assert http_unmounted.json() == {"success": True, "name": http_mcp_name}

    print("ISSUE_4136_E2E_PERF " + json.dumps(perf, sort_keys=True))
