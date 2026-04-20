"""Rate-limit integration test for MCP HTTP (#3779, AC 4)."""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def _require_seeded(monkeypatch):
    if os.environ.get("MCP_HTTP_SEEDED_ZONES") != "true":
        pytest.skip("Requires pre-seeded zones (see conftest).")
    monkeypatch.setenv("MCP_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("NEXUS_MCP_RATE_LIMIT_AUTHENTICATED", "20/minute")


@pytest.mark.asyncio
async def test_burst_triggers_429(mcp_http_base_url: str) -> None:
    token = "sk-zone01_u_k_" + "a" * 32
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "nexus_grep", "arguments": {"query": "x"}},
    }
    async with httpx.AsyncClient() as client:

        async def _one() -> int:
            resp = await client.post(
                f"{mcp_http_base_url}/mcp",
                headers={"Authorization": f"Bearer {token}"},
                json=body,
                timeout=10.0,
            )
            return resp.status_code

        statuses = await asyncio.gather(*[_one() for _ in range(50)])

    assert statuses.count(200) <= 25, "expected some 429s within the minute window"
    assert statuses.count(429) >= 20, f"expected ≥20 429 responses, got {statuses.count(429)}"


@pytest.mark.asyncio
async def test_different_tokens_isolated(mcp_http_base_url: str) -> None:
    token_a = "sk-zone01_u_k_" + "a" * 32
    token_b = "sk-zone02_u_k_" + "a" * 32
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "nexus_grep", "arguments": {"query": "x"}},
    }
    async with httpx.AsyncClient() as client:
        for _ in range(25):
            await client.post(
                f"{mcp_http_base_url}/mcp",
                headers={"Authorization": f"Bearer {token_a}"},
                json=body,
                timeout=10.0,
            )
        # token_b should still be under its own quota.
        resp_b = await client.post(
            f"{mcp_http_base_url}/mcp",
            headers={"Authorization": f"Bearer {token_b}"},
            json=body,
            timeout=10.0,
        )
        assert resp_b.status_code == 200
