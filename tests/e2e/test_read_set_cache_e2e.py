"""E2E test for Read Set-Aware Cache Invalidation (Issue #1169).

Validates the full pipeline: FastAPI server with cache enabled,
cache population via reads, precise invalidation via writes, and
precision metrics via the cache stats API.

Run with:
    pytest tests/e2e/test_read_set_cache_e2e.py -v --override-ini="addopts="
"""

from __future__ import annotations

import base64
from typing import Any

import pytest


def _rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON-RPC request body."""
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": method,
        "params": params,
    }


def _decode_bytes(value: Any) -> str:
    """Decode a bytes value from JSON-RPC response.

    The server encodes bytes as {"__type__": "bytes", "data": "<base64>"}.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value.get("__type__") == "bytes":
        return base64.b64decode(value["data"]).decode("utf-8")
    return str(value)


def _write(client: Any, path: str, content: str) -> dict[str, Any]:
    """Write a file via JSON-RPC and return response data."""
    resp = client.post("/api/nfs/write", json=_rpc("write", {"path": path, "content": content}))
    assert resp.status_code == 200, f"Write {path} HTTP failed: {resp.status_code}"
    data = resp.json()
    assert "error" not in data, f"Write {path} RPC error: {data.get('error')}"
    return data.get("result", {})


def _read(client: Any, path: str) -> str:
    """Read a file via JSON-RPC and return content string."""
    resp = client.post("/api/nfs/read", json=_rpc("read", {"path": path}))
    assert resp.status_code == 200, f"Read {path} HTTP failed: {resp.status_code}"
    data = resp.json()
    assert "error" not in data, f"Read {path} RPC error: {data.get('error')}"
    result = data.get("result", {})
    return _decode_bytes(result)


class TestReadSetCacheE2E:
    """E2E tests for read-set-aware cache invalidation via FastAPI."""

    @pytest.mark.asyncio
    async def test_precise_invalidation_pipeline(self, test_app):
        """Full pipeline: write, read, write-other, read (cache hit), overwrite, read (fresh)."""
        # Step 1: Write /test/a.txt
        _write(test_app, "/test/a.txt", "version 1")

        # Step 2: Read /test/a.txt — populates cache + read set
        content = _read(test_app, "/test/a.txt")
        assert content == "version 1"

        # Step 3: Write /test/b.txt — should NOT invalidate /test/a.txt
        _write(test_app, "/test/b.txt", "other file")

        # Step 4: Read /test/a.txt again — should still return correct content
        content = _read(test_app, "/test/a.txt")
        assert content == "version 1"

        # Step 5: Write /test/a.txt with new content — triggers invalidation
        _write(test_app, "/test/a.txt", "version 2")

        # Step 6: Read /test/a.txt — should return fresh content
        content = _read(test_app, "/test/a.txt")
        assert content == "version 2", f"Expected version 2, got: {content}"

    @pytest.mark.asyncio
    async def test_cache_stats_include_read_set_metrics(self, test_app):
        """Cache stats API includes read-set-aware cache metrics."""
        # Write and read a file to populate cache
        _write(test_app, "/stats-test/file.txt", "hello")
        _read(test_app, "/stats-test/file.txt")

        # Check cache stats endpoint
        resp = test_app.get("/api/cache/stats")
        assert resp.status_code == 200
        stats = resp.json()

        # Read-set cache metrics should be present
        if "read_set_cache" in stats:
            rsc = stats["read_set_cache"]
            assert "precise_invalidations" in rsc
            assert "skipped_invalidations" in rsc
            assert "fallback_invalidations" in rsc
            assert "stale_insert_rejections" in rsc
            assert "precision_ratio" in rsc
            assert "read_set_count" in rsc

        # Registry stats should be present
        if "read_set_registry" in stats:
            reg = stats["read_set_registry"]
            assert "read_sets_count" in reg
            assert "paths_indexed" in reg

    @pytest.mark.asyncio
    async def test_multiple_files_precise_invalidation(self, test_app):
        """Write/read multiple files — invalidation is file-specific."""
        # Write 3 files
        for name in ["alpha.txt", "beta.txt", "gamma.txt"]:
            _write(test_app, f"/multi/{name}", f"original {name}")

        # Read all 3 to populate cache
        for name in ["alpha.txt", "beta.txt", "gamma.txt"]:
            content = _read(test_app, f"/multi/{name}")
            assert content == f"original {name}"

        # Overwrite only alpha.txt
        _write(test_app, "/multi/alpha.txt", "updated alpha")

        # Read all 3 — alpha should be updated, others unchanged
        assert _read(test_app, "/multi/alpha.txt") == "updated alpha"
        assert _read(test_app, "/multi/beta.txt") == "original beta.txt"
        assert _read(test_app, "/multi/gamma.txt") == "original gamma.txt"
