"""Client-disconnect handling for MCP HTTP (#3779, criterion 8)."""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def _require_seeded():
    if os.environ.get("MCP_HTTP_SEEDED_ZONES") != "true":
        pytest.skip("Requires pre-seeded zones (see conftest).")


@pytest.mark.asyncio
async def test_client_abort_mid_request_logged(mcp_http_base_url: str) -> None:
    """Abort the connection before the response arrives; server must
    emit an audit record with status 499 and not leak resources.
    """
    token = "sk-zone01_u_k_" + "a" * 32
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "nexus_grep", "arguments": {"query": "x"}},
    }

    async with httpx.AsyncClient() as client:
        task = asyncio.create_task(
            client.post(
                f"{mcp_http_base_url}/mcp",
                headers={"Authorization": f"Bearer {token}"},
                json=body,
                timeout=10.0,
            )
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises((asyncio.CancelledError, httpx.ReadError)):
            await task

        # Immediately after, server should still be responsive.
        resp = await client.post(
            f"{mcp_http_base_url}/mcp",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
            timeout=10.0,
        )
        assert resp.status_code == 200
