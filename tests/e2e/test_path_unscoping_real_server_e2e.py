"""E2E test for path unscoping with REAL FastAPI subprocess server (#1202).

Starts an actual `nexus serve` subprocess, writes files with zone-scoped
internal paths, then makes real HTTP requests to verify paths are unscoped.

This is the most production-like test — real HTTP server, real network
requests, real NexusFS with RaftMetadataStore.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest


def _rpc_call(client: httpx.Client, method: str, params: dict | None = None) -> dict:
    """Make a real HTTP RPC call and return the result."""
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params or {},
        }
    )
    resp = client.post(
        f"/api/nfs/{method}",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200, f"RPC {method} failed: {resp.text}"
    data = resp.json()
    assert "result" in data, f"No result in RPC response: {data}"
    return data["result"]


@pytest.mark.e2e
class TestPathUnscopingRealServer:
    """E2E with real subprocess server: paths are unscoped in responses."""

    def test_list_strips_zone_prefix_real_server(self, test_app: httpx.Client) -> None:
        """Write zone-scoped files via real server, verify list returns clean paths."""
        # Write files using internal zone-scoped paths
        _rpc_call(
            test_app,
            "write",
            {"path": "/zone/default/user:alice/workspace/hello.txt", "content": "Hello!"},
        )
        _rpc_call(
            test_app,
            "write",
            {"path": "/zone/default/user:alice/workspace/data.csv", "content": "a,b,c"},
        )

        # List root — the actual bug scenario
        result = _rpc_call(test_app, "list", {"path": "/", "recursive": True})
        files = result["files"]
        paths = [f["path"] if isinstance(f, dict) else f for f in files]

        # No path should have internal prefix
        for p in paths:
            assert not p.startswith("/zone/"), f"BUG #1202: path has /zone/ prefix: {p}"
            assert not p.startswith("/tenant:"), f"BUG #1202: path has /tenant: prefix: {p}"

        # Clean paths should be present
        assert any("workspace/hello.txt" in p for p in paths), (
            f"Expected workspace/hello.txt in: {paths}"
        )
        assert any("workspace/data.csv" in p for p in paths), (
            f"Expected workspace/data.csv in: {paths}"
        )

    def test_list_strips_tenant_prefix_real_server(self, test_app: httpx.Client) -> None:
        """Write legacy tenant-prefixed files, verify list returns clean paths."""
        _rpc_call(
            test_app,
            "write",
            {"path": "/tenant:default/connector/gcs/file.txt", "content": "legacy data"},
        )

        result = _rpc_call(test_app, "list", {"path": "/", "recursive": True})
        files = result["files"]
        paths = [f["path"] if isinstance(f, dict) else f for f in files]

        for p in paths:
            assert not p.startswith("/tenant:"), f"BUG #1202: path has /tenant: prefix: {p}"

        assert any("connector/gcs/file.txt" in p for p in paths), (
            f"Expected connector/gcs/file.txt in: {paths}"
        )

    def test_glob_strips_prefix_real_server(self, test_app: httpx.Client) -> None:
        """glob() returns clean paths from real server."""
        _rpc_call(
            test_app,
            "write",
            {"path": "/zone/default/user:alice/workspace/app.py", "content": "import os"},
        )

        result = _rpc_call(test_app, "glob", {"pattern": "*.py", "path": "/"})
        matches = result["matches"]
        assert len(matches) >= 1

        for path in matches:
            assert not path.startswith("/zone/"), f"glob has /zone/ prefix: {path}"
            assert not path.startswith("/tenant:"), f"glob has /tenant: prefix: {path}"

    def test_grep_strips_prefix_real_server(self, test_app: httpx.Client) -> None:
        """grep() returns clean paths from real server."""
        _rpc_call(
            test_app,
            "write",
            {"path": "/zone/default/user:bob/workspace/search.py", "content": "import sys"},
        )

        result = _rpc_call(test_app, "grep", {"pattern": "import", "path": "/"})
        results = result["results"]
        assert len(results) >= 1

        for r in results:
            if isinstance(r, dict):
                for key in ("file", "path"):
                    if key in r and isinstance(r[key], str):
                        assert not r[key].startswith("/zone/"), (
                            f"grep {key} has /zone/ prefix: {r[key]}"
                        )
                        assert not r[key].startswith("/tenant:"), (
                            f"grep {key} has /tenant: prefix: {r[key]}"
                        )
