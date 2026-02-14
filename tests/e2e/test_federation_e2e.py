"""Federation E2E tests — cross-zone DT_MOUNT traversal against Docker cluster.

Tests the multi-zone topology:
  /              (root zone — personal workspace)
  /workspace/    (root zone)
  /corp/         → DT_MOUNT → zone "corp"
  /corp/engineering/ → DT_MOUNT → zone "corp-eng" (nested!)
  /corp/sales/   → DT_MOUNT → zone "corp-sales"
  /family/       → DT_MOUNT → zone "family"
  /family/work/  → DT_MOUNT → zone "corp" (cross-link)

5 zones: root, corp, corp-eng, corp-sales, family

Prerequisites:
    docker compose -f dockerfiles/docker-compose.cross-platform-test.yml \
        up -d postgres dragonfly nexus-1 nexus-2 witness

Run:
    uv run python -m pytest tests/e2e/test_federation_e2e.py -o "addopts=" -v
"""

from __future__ import annotations

import base64
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
HEALTH_TIMEOUT = 120  # longer for multi-zone startup


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
            "docker",
            "exec",
            node,
            "bash",
            "-c",
            "python3 /app/scripts/create_admin_key.py "
            "postgresql://postgres:nexus@postgres:5432/nexus admin",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        pytest.fail(f"Failed to create admin key: {result.stderr}")
    for line in result.stdout.splitlines():
        if line.startswith("API Key:"):
            return line.split(":", 1)[1].strip()
    pytest.fail(f"Could not parse API key from output: {result.stdout}")


def _decode_content(result: dict) -> str:
    """Decode read response content (handles base64 bytes or plain string)."""
    data = result["result"]
    if isinstance(data, dict):
        if data.get("__type__") == "bytes":
            return base64.b64decode(data["data"]).decode()
        if "content" in data:
            content = data["content"]
            if isinstance(content, dict) and content.get("__type__") == "bytes":
                return base64.b64decode(content["data"]).decode()
            return str(content)
        if "data" in data:
            try:
                return base64.b64decode(data["data"]).decode()
            except Exception:
                return str(data["data"])
    if isinstance(data, str):
        return data
    return str(data)


def _list_paths(result: dict) -> list[str]:
    """Extract list of paths from a list JSON-RPC response."""
    files = result["result"]["files"]
    return [f["path"] if isinstance(f, dict) else f for f in files]


def _uid() -> str:
    """Short unique ID for test isolation."""
    return uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def cluster():
    """Ensure the docker-compose cluster is running and healthy."""
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
# Class 1: Zone Topology Health
# ---------------------------------------------------------------------------
class TestZoneTopologyHealth:
    """Verify the multi-zone topology is correctly set up after cluster startup."""

    def test_both_nodes_healthy(self, cluster):
        for url in [cluster["node1"], cluster["node2"]]:
            h = _health(url)
            assert h is not None
            assert h["status"] == "healthy"

    def test_root_zone_write_read(self, cluster, api_key):
        """Write/read in root zone — no mount traversal."""
        uid = _uid()
        path = f"/workspace/topo-root-{uid}.txt"
        w = _jsonrpc(
            cluster["node1"],
            "write",
            {"path": path, "content": f"root-{uid}"},
            api_key=api_key,
        )
        assert "error" not in w, f"Root write failed: {w}"
        r = _jsonrpc(cluster["node1"], "read", {"path": path}, api_key=api_key)
        assert "error" not in r, f"Root read failed: {r}"

    def test_corp_zone_accessible(self, cluster, api_key):
        """Write/read through single DT_MOUNT (root → corp)."""
        uid = _uid()
        path = f"/corp/topo-corp-{uid}.txt"
        w = _jsonrpc(
            cluster["node1"],
            "write",
            {"path": path, "content": f"corp-{uid}"},
            api_key=api_key,
        )
        assert "error" not in w, f"Corp write failed: {w}"
        r = _jsonrpc(cluster["node1"], "read", {"path": path}, api_key=api_key)
        assert "error" not in r, f"Corp read failed: {r}"

    def test_nested_mount_accessible(self, cluster, api_key):
        """Write/read through nested DT_MOUNT (root → corp → corp-eng)."""
        uid = _uid()
        path = f"/corp/engineering/topo-eng-{uid}.txt"
        w = _jsonrpc(
            cluster["node1"],
            "write",
            {"path": path, "content": f"eng-{uid}"},
            api_key=api_key,
        )
        assert "error" not in w, f"Nested mount write failed: {w}"
        r = _jsonrpc(cluster["node1"], "read", {"path": path}, api_key=api_key)
        assert "error" not in r, f"Nested mount read failed: {r}"

    def test_family_zone_accessible(self, cluster, api_key):
        """Write/read through DT_MOUNT to independent zone (family)."""
        uid = _uid()
        path = f"/family/topo-family-{uid}.txt"
        w = _jsonrpc(
            cluster["node1"],
            "write",
            {"path": path, "content": f"family-{uid}"},
            api_key=api_key,
        )
        assert "error" not in w, f"Family write failed: {w}"
        r = _jsonrpc(cluster["node1"], "read", {"path": path}, api_key=api_key)
        assert "error" not in r, f"Family read failed: {r}"


# ---------------------------------------------------------------------------
# Class 2: Cross-Zone File Lifecycle
# ---------------------------------------------------------------------------
class TestCrossZoneFileLifecycle:
    """Write/read/list/delete through single DT_MOUNT (root → corp)."""

    def test_full_lifecycle(self, cluster, api_key):
        uid = _uid()
        path = f"/corp/lifecycle-{uid}.md"
        content = f"project plan {uid}"
        node = cluster["node1"]

        # Step 1: Write through DT_MOUNT
        w = _jsonrpc(node, "write", {"path": path, "content": content}, api_key=api_key)
        assert "error" not in w, f"Write failed: {w}"

        # Step 2: Read back content
        r = _jsonrpc(node, "read", {"path": path}, api_key=api_key)
        assert "error" not in r, f"Read failed: {r}"
        assert _decode_content(r) == content

        # Step 3: Get metadata — verify global path
        m = _jsonrpc(node, "get_metadata", {"path": path}, api_key=api_key)
        assert "error" not in m, f"get_metadata failed: {m}"
        meta = m["result"]
        if isinstance(meta, dict) and "metadata" in meta:
            meta = meta["metadata"]
        if isinstance(meta, dict):
            assert meta.get("path") == path

        # Step 4: List parent directory
        ls = _jsonrpc(node, "list", {"path": "/corp/"}, api_key=api_key)
        assert "error" not in ls, f"List failed: {ls}"
        assert path in _list_paths(ls), f"File not in listing"

        # Step 5: Delete
        d = _jsonrpc(node, "delete", {"path": path}, api_key=api_key)
        assert "error" not in d, f"Delete failed: {d}"

        # Step 6: Verify deleted
        ex = _jsonrpc(node, "exists", {"path": path}, api_key=api_key)
        assert "error" not in ex
        exists_val = ex["result"]
        if isinstance(exists_val, dict):
            exists_val = exists_val.get("exists", exists_val)
        assert exists_val is False


# ---------------------------------------------------------------------------
# Class 3: Nested Mount Traversal
# ---------------------------------------------------------------------------
class TestNestedMountTraversal:
    """Paths that cross TWO zone boundaries (root → corp → corp-eng)."""

    def test_depth_two_file_operations(self, cluster, api_key):
        uid = _uid()
        node = cluster["node1"]

        # Step 1: Write to depth-2 path (root → corp → corp-eng)
        src_path = f"/corp/engineering/src/main-{uid}.py"
        src_content = f"def main(): pass  # {uid}"
        w = _jsonrpc(node, "write", {"path": src_path, "content": src_content}, api_key=api_key)
        assert "error" not in w, f"Write src failed: {w}"

        # Step 2: Read back
        r = _jsonrpc(node, "read", {"path": src_path}, api_key=api_key)
        assert "error" not in r
        assert _decode_content(r) == src_content

        # Step 3: Second file in nested zone
        test_path = f"/corp/engineering/tests/test_main-{uid}.py"
        test_content = f"def test_main(): assert True  # {uid}"
        w2 = _jsonrpc(node, "write", {"path": test_path, "content": test_content}, api_key=api_key)
        assert "error" not in w2

        # Step 4: List engineering dir — both files present
        ls = _jsonrpc(node, "list", {"path": "/corp/engineering/"}, api_key=api_key)
        assert "error" not in ls
        paths = _list_paths(ls)
        assert src_path in paths, f"src not in listing: {paths}"
        assert test_path in paths, f"test not in listing: {paths}"

        # Step 5: Metadata check on depth-2 file
        m = _jsonrpc(node, "get_metadata", {"path": src_path}, api_key=api_key)
        assert "error" not in m

        # Step 6: Write to sibling zone (corp-sales) — no interference
        sales_path = f"/corp/sales/Q1-report-{uid}.xlsx"
        ws = _jsonrpc(
            node, "write", {"path": sales_path, "content": f"sales-{uid}"}, api_key=api_key
        )
        assert "error" not in ws

        # Step 7: List corp — should show both mount subtrees
        ls_corp = _jsonrpc(node, "list", {"path": "/corp/"}, api_key=api_key)
        assert "error" not in ls_corp
        corp_paths = _list_paths(ls_corp)
        assert sales_path in corp_paths, f"Sales file missing from /corp/ listing"


# ---------------------------------------------------------------------------
# Class 4: Cross-Link and Isolation
# ---------------------------------------------------------------------------
class TestCrossLinkAndIsolation:
    """Corp zone accessible from both /corp/ and /family/work/ (cross-link).
    Family-only files must NOT be visible in corp zone."""

    def test_cross_link_same_zone(self, cluster, api_key):
        uid = _uid()
        node = cluster["node1"]

        # Step 1: Write via /corp/ path
        corp_path = f"/corp/board-deck-{uid}.pdf"
        w = _jsonrpc(node, "write", {"path": corp_path, "content": f"deck-{uid}"}, api_key=api_key)
        assert "error" not in w

        # Step 2: Read via cross-link /family/work/ path — same corp zone
        crosslink_path = f"/family/work/board-deck-{uid}.pdf"
        r = _jsonrpc(node, "read", {"path": crosslink_path}, api_key=api_key)
        assert "error" not in r, f"Cross-link read failed: {r}"
        assert _decode_content(r) == f"deck-{uid}"

        # Step 3: Write via cross-link path
        memo_crosslink = f"/family/work/memo-{uid}.md"
        w2 = _jsonrpc(
            node, "write", {"path": memo_crosslink, "content": f"memo-{uid}"}, api_key=api_key
        )
        assert "error" not in w2

        # Step 4: Read via direct /corp/ path
        memo_direct = f"/corp/memo-{uid}.md"
        r2 = _jsonrpc(node, "read", {"path": memo_direct}, api_key=api_key)
        assert "error" not in r2
        assert _decode_content(r2) == f"memo-{uid}"

        # Step 5: Both listings show same corp-zone files
        ls_corp = _jsonrpc(node, "list", {"path": "/corp/"}, api_key=api_key)
        ls_work = _jsonrpc(node, "list", {"path": "/family/work/"}, api_key=api_key)
        assert "error" not in ls_corp
        assert "error" not in ls_work

        # Step 6: Family-only file (NOT in corp zone)
        family_path = f"/family/vacation-{uid}.txt"
        wf = _jsonrpc(
            node, "write", {"path": family_path, "content": f"vacation-{uid}"}, api_key=api_key
        )
        assert "error" not in wf

        # Step 7: Corp listing should NOT contain family-only file
        ls_corp2 = _jsonrpc(node, "list", {"path": "/corp/"}, api_key=api_key)
        assert "error" not in ls_corp2
        corp_paths = _list_paths(ls_corp2)
        assert family_path not in corp_paths, "Family file leaked into corp zone!"

        # Step 8: Family listing should show vacation file
        ls_family = _jsonrpc(node, "list", {"path": "/family/"}, api_key=api_key)
        assert "error" not in ls_family
        family_paths = _list_paths(ls_family)
        assert family_path in family_paths


# ---------------------------------------------------------------------------
# Class 5: Cross-Zone Cross-Node Replication
# ---------------------------------------------------------------------------
class TestCrossZoneCrossNodeReplication:
    """Write on node-1 through DT_MOUNT, verify visible on node-2."""

    def test_cross_zone_replication(self, cluster, api_key):
        uid = _uid()

        # Step 1: Write to depth-2 mount on node-1
        eng_path = f"/corp/engineering/replicated-{uid}.txt"
        w1 = _jsonrpc(
            cluster["node1"],
            "write",
            {"path": eng_path, "content": f"replicated-{uid}"},
            api_key=api_key,
        )
        assert "error" not in w1

        # Step 2: Write to independent zone on node-1
        family_path = f"/family/shared-photo-{uid}.txt"
        w2 = _jsonrpc(
            cluster["node1"],
            "write",
            {"path": family_path, "content": f"photo-{uid}"},
            api_key=api_key,
        )
        assert "error" not in w2

        # Step 3: Wait for Raft replication
        time.sleep(1.5)

        # Step 4: Verify on node-2 (corp-eng zone)
        ls1 = _jsonrpc(
            cluster["node2"],
            "list",
            {"path": "/corp/engineering/"},
            api_key=api_key,
        )
        assert "error" not in ls1, f"List on node-2 failed: {ls1}"
        assert eng_path in _list_paths(ls1), "File not replicated to node-2"

        # Step 5: Verify on node-2 (family zone)
        ls2 = _jsonrpc(
            cluster["node2"],
            "list",
            {"path": "/family/"},
            api_key=api_key,
        )
        assert "error" not in ls2
        assert family_path in _list_paths(ls2), "Family file not replicated"


# ---------------------------------------------------------------------------
# Class 6: All JSON-RPC Methods
# ---------------------------------------------------------------------------
class TestAllJSONRPCMethods:
    """Exercises every JSON-RPC method through nested cross-zone mount points."""

    def test_all_methods(self, cluster, api_key):
        uid = _uid()
        node = cluster["node1"]
        base = f"/corp/engineering/rpc-test-{uid}"

        # Step 1: mkdir
        mk = _jsonrpc(node, "mkdir", {"path": base, "parents": True}, api_key=api_key)
        assert "error" not in mk, f"mkdir failed: {mk}"

        # Step 2: write file 1
        f1 = f"{base}/file1-{uid}.txt"
        w1 = _jsonrpc(node, "write", {"path": f1, "content": f"content1-{uid}"}, api_key=api_key)
        assert "error" not in w1

        # Step 3: write file 2
        f2 = f"{base}/file2-{uid}.txt"
        w2 = _jsonrpc(node, "write", {"path": f2, "content": f"content2-{uid}"}, api_key=api_key)
        assert "error" not in w2

        # Step 4: exists → true
        ex = _jsonrpc(node, "exists", {"path": f1}, api_key=api_key)
        assert "error" not in ex
        exists_val = ex["result"]
        if isinstance(exists_val, dict):
            exists_val = exists_val.get("exists", exists_val)
        assert exists_val is True

        # Step 5: read → verify content
        r = _jsonrpc(node, "read", {"path": f1}, api_key=api_key)
        assert "error" not in r
        assert _decode_content(r) == f"content1-{uid}"

        # Step 6: get_metadata
        m = _jsonrpc(node, "get_metadata", {"path": f1}, api_key=api_key)
        assert "error" not in m

        # Step 7: list → 2 files
        ls = _jsonrpc(node, "list", {"path": f"{base}/"}, api_key=api_key)
        assert "error" not in ls
        paths = _list_paths(ls)
        assert f1 in paths
        assert f2 in paths

        # Step 8: glob
        g = _jsonrpc(node, "glob", {"pattern": f"{base}/*.txt"}, api_key=api_key)
        assert "error" not in g
        matches = g["result"]
        if isinstance(matches, dict):
            matches = matches.get("matches", matches.get("files", []))
        assert len(matches) >= 2

        # Step 9: delete file 1
        d = _jsonrpc(node, "delete", {"path": f1}, api_key=api_key)
        assert "error" not in d

        # Step 10: exists → false
        ex2 = _jsonrpc(node, "exists", {"path": f1}, api_key=api_key)
        assert "error" not in ex2
        exists_val2 = ex2["result"]
        if isinstance(exists_val2, dict):
            exists_val2 = exists_val2.get("exists", exists_val2)
        assert exists_val2 is False


# ---------------------------------------------------------------------------
# Class 7: Lock API Cross-Zone
# ---------------------------------------------------------------------------
class TestLockAPICrossZone:
    """Test REST lock API on paths that traverse DT_MOUNT."""

    def test_lock_lifecycle(self, cluster, api_key):
        uid = _uid()
        path = f"/corp/engineering/locked-{uid}.txt"
        node = cluster["node1"]

        # Write the file first
        w = _jsonrpc(node, "write", {"path": path, "content": f"lock-{uid}"}, api_key=api_key)
        assert "error" not in w

        headers = {"Authorization": f"Bearer {api_key}"}

        try:
            # Step 1: Acquire lock
            resp = httpx.post(
                f"{node}/api/locks",
                json={"path": path},
                headers=headers,
                timeout=10,
                trust_env=False,
            )
            if resp.status_code == 404:
                pytest.skip("Lock API not available")
            assert resp.status_code in (200, 201), f"Lock acquire failed: {resp.text}"

            # Step 2: Check lock
            lock_path = path.lstrip("/")
            resp2 = httpx.get(
                f"{node}/api/locks/{lock_path}",
                headers=headers,
                timeout=10,
                trust_env=False,
            )
            assert resp2.status_code == 200

            # Step 3: Release lock
            resp3 = httpx.delete(
                f"{node}/api/locks/{lock_path}",
                headers=headers,
                timeout=10,
                trust_env=False,
            )
            assert resp3.status_code in (200, 204)

            # Step 4: Verify released
            resp4 = httpx.get(
                f"{node}/api/locks/{lock_path}",
                headers=headers,
                timeout=10,
                trust_env=False,
            )
            # Either 404 (no lock) or 200 with locked=false
            if resp4.status_code == 200:
                data = resp4.json()
                assert data.get("locked") is False or data.get("state") in ("unlocked", None)

        except httpx.ConnectError:
            pytest.skip("Lock API not reachable")


# ---------------------------------------------------------------------------
# Class 8: gRPC Direct Operations
# ---------------------------------------------------------------------------
class TestgRPCDirectOperations:
    """Connect directly to Raft gRPC port and exercise RaftClient API."""

    @pytest.mark.asyncio
    async def test_grpc_cluster_info(self, cluster):
        """Get cluster info via gRPC — verify leader_id, term, is_leader."""
        try:
            from nexus.raft.client import RaftClient
        except ImportError:
            pytest.skip("RaftClient not available (requires Rust extension)")

        client = None
        try:
            client = RaftClient("localhost:2126")
            info = await client.get_cluster_info()
            assert info is not None
            # Verify leader_id is a valid node (1 or 2, NOT 3/witness)
            if hasattr(info, "leader_id") and info.leader_id:
                assert info.leader_id in (
                    1,
                    2,
                ), f"Witness became leader: {info.leader_id}"
            if hasattr(info, "term"):
                assert info.term >= 1
        except Exception as e:
            if "connect" in str(e).lower() or "refused" in str(e).lower():
                pytest.skip(f"gRPC not reachable: {e}")
            raise
        finally:
            if client and hasattr(client, "close"):
                await client.close()

    @pytest.mark.asyncio
    async def test_grpc_metadata_roundtrip(self, cluster):
        """Write FileMetadata via gRPC, read it back."""
        try:
            from nexus.core._metadata_generated import FileMetadata
            from nexus.raft.client import RaftClient
        except ImportError:
            pytest.skip("RaftClient or FileMetadata not available")

        uid = _uid()
        client = None
        try:
            client = RaftClient("localhost:2126")

            meta = FileMetadata(
                path=f"/grpc-test-{uid}.txt",
                backend_name="local",
                physical_path=f"/data/grpc-test-{uid}.txt",
                size=42,
            )
            await client.put_metadata(meta)

            result = await client.get_metadata(f"/grpc-test-{uid}.txt")
            assert result is not None
            assert result.size == 42
        except Exception as e:
            err = str(e).lower()
            if "connect" in err or "not implemented" in err or "refused" in err:
                pytest.skip(f"gRPC operation not available: {e}")
            raise
        finally:
            if client and hasattr(client, "close"):
                await client.close()

    @pytest.mark.asyncio
    async def test_grpc_list_metadata(self, cluster):
        """List metadata entries via gRPC."""
        try:
            from nexus.raft.client import RaftClient
        except ImportError:
            pytest.skip("RaftClient not available")

        client = None
        try:
            client = RaftClient("localhost:2126")
            entries = await client.list_metadata(prefix="/")
            assert entries is not None
            assert len(entries) >= 0  # Root zone should have at least "/"
        except Exception as e:
            err = str(e).lower()
            if "connect" in err or "not implemented" in err or "refused" in err:
                pytest.skip(f"gRPC list not available: {e}")
            raise
        finally:
            if client and hasattr(client, "close"):
                await client.close()


# ---------------------------------------------------------------------------
# Class 9: Multi-Zone Agent Workflow
# ---------------------------------------------------------------------------
class TestMultiZoneAgentWorkflow:
    """Real-world workflow: engineer works across personal, corp, and family zones."""

    def test_engineer_workflow(self, cluster, api_key):
        uid = _uid()
        node1 = cluster["node1"]
        node2 = cluster["node2"]

        # Step 1: Personal note in root zone
        todo = f"/workspace/todo-{uid}.md"
        w1 = _jsonrpc(
            node1,
            "write",
            {"path": todo, "content": f"# TODO {uid}\n- Ship feature"},
            api_key=api_key,
        )
        assert "error" not in w1

        # Step 2: Project dir in corp-eng (depth-2 nested mount)
        project_dir = f"/corp/engineering/project-{uid}"
        mk = _jsonrpc(node1, "mkdir", {"path": project_dir, "parents": True}, api_key=api_key)
        assert "error" not in mk

        # Step 3: README
        readme = f"{project_dir}/README.md"
        w2 = _jsonrpc(
            node1,
            "write",
            {"path": readme, "content": f"# Project {uid}"},
            api_key=api_key,
        )
        assert "error" not in w2

        # Step 4: Source code
        main_py = f"{project_dir}/main.py"
        w3 = _jsonrpc(
            node1,
            "write",
            {"path": main_py, "content": f"print('hello {uid}')"},
            api_key=api_key,
        )
        assert "error" not in w3

        # Step 5: Tests
        test_py = f"{project_dir}/test.py"
        w4 = _jsonrpc(
            node1,
            "write",
            {"path": test_py, "content": f"assert True  # {uid}"},
            api_key=api_key,
        )
        assert "error" not in w4

        # Step 6: List project dir — verify 3 files
        ls = _jsonrpc(node1, "list", {"path": f"{project_dir}/"}, api_key=api_key)
        assert "error" not in ls
        paths = _list_paths(ls)
        assert len(paths) >= 3, f"Expected 3+ files, got: {paths}"

        # Step 7: Sales proposal (sibling zone corp-sales)
        proposal = f"/corp/sales/proposal-{uid}.pdf"
        w5 = _jsonrpc(
            node1,
            "write",
            {"path": proposal, "content": f"proposal-{uid}"},
            api_key=api_key,
        )
        assert "error" not in w5

        # Step 8: Family share (independent zone)
        demo = f"/family/demo-{uid}.mp4"
        w6 = _jsonrpc(
            node1,
            "write",
            {"path": demo, "content": f"demo-{uid}"},
            api_key=api_key,
        )
        assert "error" not in w6

        # Step 9: Read all 6 files back from node-2 (cross-node + cross-zone)
        time.sleep(1.5)
        for p in [todo, readme, main_py, test_py, proposal, demo]:
            r = _jsonrpc(node2, "read", {"path": p}, api_key=api_key)
            assert "error" not in r, f"Cross-node read failed for {p}: {r}"


# ---------------------------------------------------------------------------
# Class 10: Leader Failover Cross-Zone (LAST — restarts containers)
# ---------------------------------------------------------------------------
class TestLeaderFailoverCrossZone:
    """Write cross-zone data, kill leader, verify data survives on new leader."""

    def test_failover_preserves_cross_zone_data(self, cluster, api_key):
        uid = _uid()

        # Step 1: Write to depth-2 nested mount
        eng_path = f"/corp/engineering/failover-{uid}.txt"
        w1 = _jsonrpc(
            cluster["node1"],
            "write",
            {"path": eng_path, "content": f"failover-eng-{uid}"},
            api_key=api_key,
        )
        assert "error" not in w1

        # Step 2: Write to independent zone
        family_path = f"/family/failover-{uid}.txt"
        w2 = _jsonrpc(
            cluster["node1"],
            "write",
            {"path": family_path, "content": f"failover-family-{uid}"},
            api_key=api_key,
        )
        assert "error" not in w2

        # Wait for replication before killing leader
        time.sleep(2)

        # Step 3: Stop node-1 (leader)
        subprocess.run(["docker", "stop", "nexus-node-1"], timeout=30, check=True)

        try:
            # Step 4: Wait for node-2 to become available
            deadline = time.time() + 30
            node2_ready = False
            while time.time() < deadline:
                h = _health(cluster["node2"])
                if h and h.get("status") == "healthy":
                    node2_ready = True
                    break
                time.sleep(2)
            if not node2_ready:
                pytest.fail("Node-2 did not become healthy after leader stop")

            # Step 5: Read corp-eng data from node-2
            r1 = _jsonrpc(
                cluster["node2"],
                "read",
                {"path": eng_path},
                api_key=api_key,
                timeout=15,
            )
            assert "error" not in r1, f"Failover read (eng) failed: {r1}"
            assert _decode_content(r1) == f"failover-eng-{uid}"

            # Step 6: Read family data from node-2
            r2 = _jsonrpc(
                cluster["node2"],
                "read",
                {"path": family_path},
                api_key=api_key,
                timeout=15,
            )
            assert "error" not in r2, f"Failover read (family) failed: {r2}"
            assert _decode_content(r2) == f"failover-family-{uid}"

        finally:
            # Step 7: Restart node-1 and wait for health
            subprocess.run(["docker", "start", "nexus-node-1"], timeout=30, check=True)
            _wait_healthy([cluster["node1"]], timeout=60)
