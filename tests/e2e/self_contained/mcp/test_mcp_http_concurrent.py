"""Concurrent multi-client MCP HTTP test (#3779, AC 1 & 2).

Spins up MCP server with `MCP_TRANSPORT=http` behind real Dragonfly,
issues 10 simultaneous `nexus_grep` calls with distinct zone tokens,
and asserts no cross-zone leakage. Also records wall time as the
measurement gate for Q5 (BM25S lock contention).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.e2e


async def _grep(client: httpx.AsyncClient, base: str, token: str, query: str) -> Any:
    resp = await client.post(
        f"{base}/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "nexus_grep", "arguments": {"query": query}},
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


@pytest.mark.asyncio
async def test_ten_clients_get_zone_scoped_results(mcp_http_base_url: str, tmp_path) -> None:
    # Provision 10 zones; each seeds a unique marker file the others
    # should NOT see through nexus_grep.
    zones = [
        ("zone-01", "sk-zone01_u_k_" + "a" * 32, "MARKER_01"),
        ("zone-02", "sk-zone02_u_k_" + "a" * 32, "MARKER_02"),
        ("zone-03", "sk-zone03_u_k_" + "a" * 32, "MARKER_03"),
        ("zone-04", "sk-zone04_u_k_" + "a" * 32, "MARKER_04"),
        ("zone-05", "sk-zone05_u_k_" + "a" * 32, "MARKER_05"),
        ("zone-06", "sk-zone06_u_k_" + "a" * 32, "MARKER_06"),
        ("zone-07", "sk-zone07_u_k_" + "a" * 32, "MARKER_07"),
        ("zone-08", "sk-zone08_u_k_" + "a" * 32, "MARKER_08"),
        ("zone-09", "sk-zone09_u_k_" + "a" * 32, "MARKER_09"),
        ("zone-10", "sk-zone10_u_k_" + "a" * 32, "MARKER_10"),
    ]
    # Seed: the test harness (conftest) must provision these zones + tokens
    # and write `$marker` into each zone's /tmp/marker.txt before this runs.
    # If not yet provisioned, skip with a clear message.
    _require_seeded_zones(zones)

    async with httpx.AsyncClient() as client:
        t0 = time.monotonic()
        tasks = [_grep(client, mcp_http_base_url, token, marker) for _, token, marker in zones]
        results = await asyncio.gather(*tasks)
        elapsed = time.monotonic() - t0

    # Each client sees only its own marker.
    for (_, _, marker), result in zip(zones, results, strict=True):
        text = json.dumps(result)
        assert marker in text, f"expected {marker} in own zone result"
        for _, _, other in zones:
            if other == marker:
                continue
            assert other not in text, f"cross-zone leak: {other} in {marker}'s result"

    # Measurement: wall time << 10 * single_request_time if BM25S lock isn't
    # a global bottleneck. Record; fail only if > 3× the single-request budget.
    # Single grep is ~1s on warm index; 10 parallel should finish well under 10s.
    single_budget_s = float(os.environ.get("MCP_HTTP_SINGLE_BUDGET_S", "1.0"))
    assert elapsed < single_budget_s * 3, (
        f"10-way concurrency took {elapsed:.2f}s — suggests global lock; "
        f"inspect BM25S lock contention (Q5 measurement)."
    )


def _require_seeded_zones(zones: list[tuple[str, str, str]]) -> None:
    """Skip the test if the nexus stack lacks the expected zones/tokens.

    The conftest fixture in this dir is responsible for seeding; this
    guard produces a clear skip message instead of confusing failures.
    """
    if os.environ.get("MCP_HTTP_SEEDED_ZONES") != "true":
        pytest.skip(
            "MCP HTTP concurrent test requires pre-seeded zones + tokens. "
            "Set MCP_HTTP_SEEDED_ZONES=true and provision via conftest."
        )
