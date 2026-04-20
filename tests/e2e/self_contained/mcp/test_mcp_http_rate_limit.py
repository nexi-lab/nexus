"""Rate-limit integration test for MCP HTTP (#3779, AC 4).

Requires the MCP server to run with MCP_RATE_LIMIT_ENABLED=true and
NEXUS_MCP_RATE_LIMIT_AUTHENTICATED tuned low (e.g. 20/minute) so a
single-process burst produces observable 429s within test time.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

pytestmark = pytest.mark.e2e


_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "nexus_grep", "arguments": {"query": "x"}},
}


@pytest.mark.asyncio
async def test_burst_triggers_429(mcp_http_base_url: str, seeded_zones) -> None:
    token = seeded_zones[0]["api_key"]
    async with httpx.AsyncClient() as client:

        async def _one() -> int:
            resp = await client.post(
                f"{mcp_http_base_url}/mcp",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json, text/event-stream",
                },
                json=_BODY,
                timeout=10.0,
            )
            return resp.status_code

        statuses = await asyncio.gather(*[_one() for _ in range(50)])

    ok = statuses.count(200)
    throttled = statuses.count(429)
    assert throttled >= 20, (
        f"expected ≥20 429 responses within the minute window; got {throttled} "
        f"(200={ok}, 429={throttled}). Check MCP_RATE_LIMIT_ENABLED + "
        f"NEXUS_MCP_RATE_LIMIT_AUTHENTICATED is tuned low (≤20/minute)."
    )


@pytest.mark.asyncio
async def test_different_tokens_isolated(mcp_http_base_url: str, seeded_zones) -> None:
    token_a = seeded_zones[0]["api_key"]
    token_b = seeded_zones[1]["api_key"]
    async with httpx.AsyncClient() as client:
        # Saturate token A's bucket.
        for _ in range(25):
            await client.post(
                f"{mcp_http_base_url}/mcp",
                headers={
                    "Authorization": f"Bearer {token_a}",
                    "Accept": "application/json, text/event-stream",
                },
                json=_BODY,
                timeout=10.0,
            )
        # Token B must still be under its own quota.
        resp_b = await client.post(
            f"{mcp_http_base_url}/mcp",
            headers={
                "Authorization": f"Bearer {token_b}",
                "Accept": "application/json, text/event-stream",
            },
            json=_BODY,
            timeout=10.0,
        )
        assert resp_b.status_code == 200, (
            f"token B (zone={seeded_zones[1]['zone_id']}) got {resp_b.status_code}; "
            f"should be unaffected by token A's burst"
        )
