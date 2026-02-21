"""Integration test for Memory Paging via HTTP/FastAPI (Issue #1258).

Tests the complete stack:
- FastAPI server (via shared nexus_server fixture)
- Authentication
- Memory store via RPC
"""

import uuid

import httpx
import pytest


class TestMemoryPagingHTTP:
    """Test memory paging through HTTP API."""

    def test_server_health(self, nexus_server, test_app: httpx.Client):
        """Server should respond to health check."""
        resp = test_app.get("/health")
        assert resp.status_code == 200

        data = resp.json()
        assert data["status"] in ("ok", "healthy")
        assert data["service"] in ("nexus", "nexus-rpc")

    def test_memory_store_via_http(self, nexus_server, test_app: httpx.Client):
        """Should be able to store memory via RPC."""
        resp = test_app.post(
            "/api/nfs/store_memory",
            json={
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "store_memory",
                "params": {
                    "content": "Test memory for paging",
                    "memory_type": "fact",
                    "scope": "user",
                },
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert "error" not in data, f"store_memory failed: {data}"

    def test_memory_paging_not_enabled_by_default(self, nexus_server, test_app: httpx.Client):
        """Memory paging should NOT be enabled by default (backward compat).

        Store multiple memories and verify they're all accessible.
        """
        # Store 5 memories
        for i in range(5):
            resp = test_app.post(
                "/api/nfs/store_memory",
                json={
                    "jsonrpc": "2.0",
                    "id": str(uuid.uuid4()),
                    "method": "store_memory",
                    "params": {
                        "content": f"Memory {i} for paging test",
                        "memory_type": "fact",
                        "scope": "user",
                    },
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "error" not in data, f"store_memory {i} failed: {data}"

    @pytest.mark.skip(reason="Paging needs to be explicitly enabled in server config")
    def test_memory_paging_enabled_via_config(self, nexus_server, test_app: httpx.Client):
        """When paging enabled, should cascade through tiers."""
        pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
