"""Zone isolation regression tests for /api/v2 + MCP glob (#3779 follow-up).

Reproduces two bugs exposed by PR #3779's concurrent e2e test:

1. REST GET /api/v2/files/list via zone-scoped key returned files from
   *other* zones (sys_readdir in nexus_fs.py did not apply a zone-column
   filter to metadata.list_iter results).

2. MCP nexus_glob via zone-scoped key returned zero items for files
   owned by the caller's zone (search_service.list prepended
   ``/zone/<id>/`` to the list_prefix, but V2 API writes store flat
   paths with zone_id as a metadata column — the prefix never matched).

These tests drive the fix: sys_readdir must filter by zone_id column;
search.list() must stop prepending the /zone/ prefix and filter the
same way.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

from .conftest import mcp_http_call

pytestmark = pytest.mark.e2e


def _v2_api_base_url() -> str:
    return os.environ.get("NEXUS_ADMIN_URL", "http://localhost:38630")


def test_rest_list_does_not_leak_cross_zone(seeded_zones) -> None:
    """A zone key's /api/v2/files/list must not return sibling zone files."""
    own = seeded_zones[0]
    sibling_markers = [f"marker-{z['zone_id']}.txt" for z in seeded_zones[1:]]
    with httpx.Client(base_url=_v2_api_base_url(), timeout=15.0) as client:
        resp = client.get(
            "/api/v2/files/list",
            params={"path": "/", "limit": 200},
            headers={"Authorization": f"Bearer {own['api_key']}"},
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        names = {item.get("name", "") for item in items}
    leaked = [m for m in sibling_markers if m in names]
    assert not leaked, f"zone {own['zone_id']} key saw sibling zone markers via V2 list: {leaked!r}"


@pytest.mark.asyncio
async def test_mcp_glob_finds_own_zone_marker(mcp_http_base_url: str, seeded_zones) -> None:
    """A zone key's MCP nexus_glob must find its own marker file."""
    own = seeded_zones[0]
    own_marker = f"marker-{own['zone_id']}.txt"
    payload = await mcp_http_call(
        mcp_http_base_url,
        own["api_key"],
        "tools/call",
        {"name": "nexus_glob", "arguments": {"pattern": "/marker-*.txt"}},
        timeout=30.0,
    )

    text = json.dumps(payload)
    assert own_marker in text, (
        f"zone {own['zone_id']} MCP glob did not return its own marker {own_marker}: {text[:400]}"
    )


@pytest.mark.asyncio
async def test_mcp_glob_does_not_leak_sibling_zone_marker(
    mcp_http_base_url: str, seeded_zones
) -> None:
    """A zone key's MCP glob must not return any sibling zone's marker."""
    own = seeded_zones[0]
    payload = await mcp_http_call(
        mcp_http_base_url,
        own["api_key"],
        "tools/call",
        {"name": "nexus_glob", "arguments": {"pattern": "/marker-*.txt"}},
        timeout=30.0,
    )

    text = json.dumps(payload)
    leaks = [
        f"marker-{z['zone_id']}.txt"
        for z in seeded_zones[1:]
        if f"marker-{z['zone_id']}.txt" in text
    ]
    assert not leaks, f"zone {own['zone_id']} MCP glob leaked sibling markers: {leaks!r}"
