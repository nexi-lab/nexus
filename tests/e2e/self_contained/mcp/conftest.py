"""Fixtures for MCP integration tests."""

import os

import pytest


@pytest.fixture(autouse=True)
def isolate_mcp_integration_tests(monkeypatch):
    """Isolate MCP integration tests from environment pollution.

    This fixture clears NEXUS environment variables that could
    affect the test configuration and cause intermittent failures.
    """
    # Clear all NEXUS environment variables
    env_vars_to_clear = [
        "NEXUS_BACKEND",
        "NEXUS_DATA_DIR",
        "NEXUS_GCS_BUCKET_NAME",
        "NEXUS_GCS_PROJECT_ID",
        "NEXUS_DATABASE_URL",
        "NEXUS_URL",
        "NEXUS_API_KEY",
        "NEXUS_PROFILE",
    ]

    for var in env_vars_to_clear:
        monkeypatch.delenv(var, raising=False)

    yield


async def mcp_http_call(
    base_url: str,
    token: str,
    method: str,
    params: dict | None = None,
    *,
    timeout: float = 30.0,
) -> dict:
    """Minimal MCP streamable-HTTP client (initialize + one call).

    Returns the decoded JSON-RPC payload from the SSE stream.
    Avoids the `mcp.client.streamable_http` SDK which hangs against
    FastMCP 2.13 in some environments.
    """
    import json

    import httpx

    headers_base = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1"},
        },
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST", f"{base_url}/mcp", headers=headers_base, json=init_body
        ) as resp:
            resp.raise_for_status()
            session_id = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
            # Drain the initialize SSE response so FastMCP marks the session ready.
            async for _ in resp.aiter_lines():
                pass
        assert session_id, "initialize did not return mcp-session-id"

        # Send initialized notification (required by MCP spec before tool calls).
        await client.post(
            f"{base_url}/mcp",
            headers={**headers_base, "Mcp-Session-Id": session_id},
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )

        # Issue the real call.
        body = {"jsonrpc": "2.0", "id": 2, "method": method}
        if params is not None:
            body["params"] = params
        async with client.stream(
            "POST",
            f"{base_url}/mcp",
            headers={**headers_base, "Mcp-Session-Id": session_id},
            json=body,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    return json.loads(line[6:])
    raise RuntimeError("no data event in SSE response")


@pytest.fixture
def mcp_http_base_url() -> str:
    """Base URL for the MCP HTTP transport under test.

    Override via MCP_HTTP_URL env var. Default assumes a running nexus
    stack with MCP_TRANSPORT=http on port 8081.
    """
    return os.environ.get("MCP_HTTP_URL", "http://localhost:8081")


@pytest.fixture(scope="session")
def seeded_zones():
    """Provision 10 zones + per-zone API keys + marker files.

    Yields a list of ``{"zone_id", "api_key", "marker"}`` dicts.

    Requires the nexus stack to be running with admin credentials
    exported as ``NEXUS_ADMIN_URL`` (default ``http://localhost:38630``)
    and ``NEXUS_ADMIN_KEY`` (the static API key from ``nexus env``).

    Skips if the admin key is not set. Cleans up on teardown by
    deleting the created zones (keys cascade).
    """
    import httpx

    admin_url = os.environ.get("NEXUS_ADMIN_URL", "http://localhost:38630")
    admin_key = os.environ.get("NEXUS_ADMIN_KEY")
    if not admin_key:
        pytest.skip(
            "seeded_zones requires NEXUS_ADMIN_URL + NEXUS_ADMIN_KEY "
            "pointing at a running nexus stack"
        )

    headers = {"Authorization": f"Bearer {admin_key}"}
    zones: list[dict] = []
    created_zone_ids: list[str] = []

    with httpx.Client(base_url=admin_url, headers=headers, timeout=15.0) as client:
        # Zone IDs must fit in 8 chars (sk-token zone segment cap).
        for i in range(10):
            zone_id = f"mcph{i:02d}"
            marker = f"MARKER_MCP_{i:02d}"

            # Best-effort zone create. Some presets (`shared`) lack a DB auth
            # provider and return 503 on /api/zones; the demo preset's /api/v2
            # key endpoint accepts any zone_id and sets up ReBAC scoping on
            # the first key creation anyway. 200/201/409/503 all acceptable.
            resp = client.post(
                "/api/zones",
                json={"name": f"mcp-http-{i:02d}", "zone_id": zone_id},
            )
            if resp.status_code in (200, 201):
                created_zone_ids.append(zone_id)
            elif resp.status_code not in (409, 503):
                resp.raise_for_status()

            # Issue a zone-scoped API key with viewer + editor grants on root.
            key_resp = client.post(
                "/api/v2/auth/keys",
                json={
                    "zone_id": zone_id,
                    "label": f"mcp-http-test-{i:02d}",
                    "is_admin": False,
                    "grants": [
                        {"path": "/", "role": "editor"},
                    ],
                },
            )
            key_resp.raise_for_status()
            api_key = key_resp.json().get("key") or key_resp.json().get("api_key")
            assert api_key, f"no key returned for zone {zone_id}: {key_resp.json()}"

            # Write marker file into the zone root using the zone's key.
            zone_headers = {"Authorization": f"Bearer {api_key}"}
            write_resp = client.post(
                "/api/v2/files/write",
                headers=zone_headers,
                json={
                    "path": f"/marker-{zone_id}.txt",
                    "content": f"{marker} contents for {zone_id}",
                },
            )
            if write_resp.status_code not in (200, 201):
                write_resp.raise_for_status()

            zones.append({"zone_id": zone_id, "api_key": api_key, "marker": marker})

        # Let zoekt/bm25s index the new files briefly before tests run.
        import time

        time.sleep(2)

        os.environ["MCP_HTTP_SEEDED_ZONES"] = "true"
        yield zones

        # Teardown: delete zones we created this session.
        for zid in created_zone_ids:
            try:
                client.delete(f"/api/zones/{zid}")
            except Exception:
                pass
        os.environ.pop("MCP_HTTP_SEEDED_ZONES", None)
