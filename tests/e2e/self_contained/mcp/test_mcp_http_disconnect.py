"""Client-disconnect handling for MCP HTTP (#3779, criterion 8).

Cancels a request mid-flight and asserts the server stays responsive
for subsequent requests using the same token.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_client_abort_mid_request_logged(mcp_http_base_url: str, seeded_zones) -> None:
    token = seeded_zones[0]["api_key"]
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
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json, text/event-stream",
                },
                json=body,
                timeout=10.0,
            )
        )
        await asyncio.sleep(0.01)
        task.cancel()
        # The cancellation may win (CancelledError/ReadError) or the
        # request may complete first (400 from FastMCP for no-session POST).
        # Both are acceptable — we only need to verify the server stays
        # responsive afterward.
        try:
            await task
        except (asyncio.CancelledError, httpx.ReadError):
            pass

        # Server must be responsive immediately after the abort. FastMCP
        # returns 400 for a raw POST without an MCP session; any response
        # other than a timeout/connection-refused proves the server is up.
        resp = await client.post(
            f"{mcp_http_base_url}/mcp",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
            json=body,
            timeout=10.0,
        )
        # 200 (healthy SSE), 400/406 (no MCP session), 429 (rate-limited but
        # responding) — all prove the server stayed up after the abort.
        assert resp.status_code in (200, 400, 406, 429), (
            f"server not responsive after client abort: got {resp.status_code}"
        )
