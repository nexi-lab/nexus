"""Concurrent multi-client MCP HTTP test (#3779, AC 1 & 2).

Uses ``mcp_http_call`` (raw streamable-HTTP session) to issue 10
concurrent ``nexus_glob`` calls with distinct zone tokens. Asserts no
cross-zone leakage and records wall time as the Q5 BM25S lock-contention
measurement.

Requires ``seeded_zones`` fixture in conftest.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from .conftest import mcp_http_call

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_ten_clients_get_zone_scoped_results(mcp_http_base_url: str, seeded_zones) -> None:
    zones = seeded_zones
    assert len(zones) == 10, f"expected 10 seeded zones, got {len(zones)}"

    async def _glob(zone: dict) -> dict:
        return await mcp_http_call(
            mcp_http_base_url,
            zone["api_key"],
            "tools/call",
            {"name": "nexus_glob", "arguments": {"pattern": "/marker-*.txt"}},
            timeout=30.0,
        )

    t0 = time.monotonic()
    results = await asyncio.gather(*(_glob(z) for z in zones), return_exceptions=True)
    elapsed = time.monotonic() - t0

    errors = [(i, r) for i, r in enumerate(results) if isinstance(r, Exception)]
    assert not errors, f"per-client errors: {errors!r}"

    # Each zone must see ONLY its own marker file.
    for zone, payload in zip(zones, results, strict=True):
        text = json.dumps(payload)
        own_marker = f"marker-{zone['zone_id']}.txt"
        assert own_marker in text, (
            f"expected own marker {own_marker} in zone {zone['zone_id']} result; got: {text[:400]}"
        )
        for other in zones:
            if other["zone_id"] == zone["zone_id"]:
                continue
            other_marker = f"marker-{other['zone_id']}.txt"
            assert other_marker not in text, (
                f"cross-zone leak: {other_marker} (from {other['zone_id']}) "
                f"visible to {zone['zone_id']}; result: {text[:400]}"
            )

    # Q5 BM25S lock-contention measurement.
    single_budget_s = float(os.environ.get("MCP_HTTP_SINGLE_BUDGET_S", "5.0"))
    assert elapsed < single_budget_s * 3, (
        f"10-way concurrency took {elapsed:.2f}s — suggests global lock; "
        f"inspect BM25S lock contention."
    )
    print(f"\n[Q5 measurement] 10-way concurrent glob wall time: {elapsed:.2f}s")
