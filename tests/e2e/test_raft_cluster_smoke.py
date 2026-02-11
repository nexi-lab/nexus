"""Raft cluster smoke tests — requires docker-compose 3-node cluster.

These tests validate the core Raft consensus behavior against a live
docker-compose cluster (nexus-1, nexus-2, witness).

Prerequisites:
    docker compose -f dockerfiles/docker-compose.cross-platform-test.yml \
        up -d postgres dragonfly nexus-1 nexus-2 witness

Run:
    uv run python -m pytest tests/e2e/test_raft_cluster_smoke.py -o "addopts=" -v

The tests are marked with @pytest.mark.docker so they can be skipped in CI
environments without Docker.
"""

from __future__ import annotations

import subprocess
import time
import uuid

import httpx
import pytest

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
NODE1_URL = "http://localhost:2026"
NODE2_URL = "http://localhost:2027"
WITNESS_URL = "http://localhost:2028"  # witness doesn't serve HTTP API
HEALTH_TIMEOUT = 60  # seconds to wait for cluster to be healthy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _jsonrpc(url: str, method: str, params: dict, *, api_key: str, timeout: float = 10) -> dict:
    """Send a JSON-RPC request and return the parsed response."""
    resp = httpx.post(
        f"{url}/api/nfs/{method}",
        json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout,
        # trust_env=False avoids proxy issues with localhost
        trust_env=False,
    )
    return resp.json()


def _health(url: str) -> dict | None:
    """Check /health endpoint. Returns None if unreachable."""
    try:
        resp = httpx.get(f"{url}/health", timeout=5, trust_env=False)
        if resp.status_code == 200:
            return resp.json()
    except httpx.ConnectError:
        pass
    return None


def _wait_healthy(urls: list[str], timeout: float = HEALTH_TIMEOUT) -> None:
    """Wait until all URLs return healthy."""
    deadline = time.time() + timeout
    for url in urls:
        while time.time() < deadline:
            h = _health(url)
            if h and h.get("status") == "healthy":
                break
            time.sleep(2)
        else:
            pytest.fail(f"Timed out waiting for {url} to become healthy")


def _create_admin_key(node: str = "nexus-node-1") -> str:
    """Create an admin API key via docker exec."""
    result = subprocess.run(
        [
            "docker", "exec", node, "bash", "-c",
            "python3 /app/scripts/create_admin_key.py "
            "postgresql://postgres:nexus@postgres:5432/nexus admin",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        pytest.fail(f"Failed to create admin key: {result.stderr}")
    # Parse "API Key: sk-..." from output
    for line in result.stdout.splitlines():
        if line.startswith("API Key:"):
            return line.split(":", 1)[1].strip()
    pytest.fail(f"Could not parse API key from output: {result.stdout}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def cluster():
    """Ensure the docker-compose cluster is running and healthy."""
    # Quick check: is node-1 already reachable?
    if _health(NODE1_URL) is None:
        pytest.skip(
            "Docker cluster not running. Start with:\n"
            "  docker compose -f dockerfiles/docker-compose.cross-platform-test.yml "
            "up -d postgres dragonfly nexus-1 nexus-2 witness"
        )

    _wait_healthy([NODE1_URL, NODE2_URL])
    return {"node1": NODE1_URL, "node2": NODE2_URL}


@pytest.fixture(scope="module")
def api_key(cluster):
    """Get an admin API key for the cluster."""
    return _create_admin_key()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestRaftClusterHealth:
    """Basic cluster health and connectivity."""

    def test_node1_healthy(self, cluster):
        h = _health(cluster["node1"])
        assert h is not None
        assert h["status"] == "healthy"

    def test_node2_healthy(self, cluster):
        h = _health(cluster["node2"])
        assert h is not None
        assert h["status"] == "healthy"

    def test_both_nodes_have_auth(self, cluster):
        for url in [cluster["node1"], cluster["node2"]]:
            h = _health(url)
            assert h["has_auth"] is True


class TestRaftWriteRead:
    """Write and read operations through Raft consensus."""

    def test_write_to_leader(self, cluster, api_key):
        """Write a file through the leader node."""
        path = f"/test-smoke-{uuid.uuid4().hex[:8]}.txt"
        content = "hello raft cluster"

        result = _jsonrpc(
            cluster["node1"], "write",
            {"path": path, "content": content},
            api_key=api_key,
        )

        assert "error" not in result, f"Write failed: {result}"
        assert "result" in result
        bw = result["result"]["bytes_written"]
        assert bw["size"] == len(content)
        assert bw["version"] == 1

    def test_read_from_leader(self, cluster, api_key):
        """Write then read back from the same node."""
        path = f"/test-read-{uuid.uuid4().hex[:8]}.txt"
        content = "read-back-test"

        # Write
        w = _jsonrpc(
            cluster["node1"], "write",
            {"path": path, "content": content},
            api_key=api_key,
        )
        assert "error" not in w

        # Read
        r = _jsonrpc(
            cluster["node1"], "read",
            {"path": path},
            api_key=api_key,
        )
        assert "error" not in r, f"Read failed: {r}"
        assert "result" in r


class TestRaftMetadataReplication:
    """Verify metadata is replicated across Raft nodes."""

    def test_metadata_visible_on_follower(self, cluster, api_key):
        """Write on leader, verify metadata visible on follower via list."""
        path = f"/test-repl-{uuid.uuid4().hex[:8]}.txt"

        # Write on node 1 (leader)
        w = _jsonrpc(
            cluster["node1"], "write",
            {"path": path, "content": "replication test"},
            api_key=api_key,
        )
        assert "error" not in w

        # Small delay for replication
        time.sleep(0.5)

        # List on node 2 (follower) — should see the file
        r = _jsonrpc(
            cluster["node2"], "list",
            {"path": "/"},
            api_key=api_key,
        )
        assert "error" not in r, f"List failed: {r}"
        files = r["result"]["files"]
        assert path in files, f"File {path} not replicated to follower. Files: {files}"


class TestRaftLeaderElection:
    """Verify leader election correctness (witness must not be leader)."""

    def test_write_to_follower_rejected(self, cluster, api_key):
        """Writing to a non-leader should return 'not leader' with leader hint."""
        path = f"/test-follower-write-{uuid.uuid4().hex[:8]}.txt"

        result = _jsonrpc(
            cluster["node2"], "write",
            {"path": path, "content": "should fail"},
            api_key=api_key,
        )

        # Expect either success (if node2 is leader) or "not leader" error
        if "error" in result:
            err_msg = result["error"]["message"]
            assert "not leader" in err_msg.lower(), f"Unexpected error: {err_msg}"
            # Leader hint should be a valid node (1 or 2, NOT 3/witness)
            assert "leader hint: Some(3)" not in err_msg, (
                "Witness (node 3) should never be leader!"
            )

    def test_witness_not_leader(self, cluster, api_key):
        """Verify that when writes fail with leader hint, witness is never the leader."""
        results = []
        for _ in range(3):
            path = f"/test-witness-check-{uuid.uuid4().hex[:8]}.txt"
            r = _jsonrpc(
                cluster["node2"], "write",
                {"path": path, "content": "witness check"},
                api_key=api_key,
            )
            if "error" in r:
                results.append(r["error"]["message"])

        for msg in results:
            assert "leader hint: Some(3)" not in msg, (
                f"Witness (node 3) became leader! Error: {msg}"
            )
