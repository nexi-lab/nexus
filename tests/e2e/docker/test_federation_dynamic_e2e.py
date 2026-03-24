"""Dynamic Federation E2E tests — build zone topology at runtime via RPC.

Unlike test_federation_e2e.py which relies on static NEXUS_FEDERATION_ZONES
env vars, these tests start from bare root-zone-only nodes and dynamically
create zones, mounts, and cross-node joins via JSON-RPC.

Target topology (built incrementally by tests):
  /              (root zone — bootstrapped at startup)
  /corp/         → DT_MOUNT → zone "corp"
  /corp/eng/     → DT_MOUNT → zone "corp-eng"   (nested)
  /corp/sales/   → DT_MOUNT → zone "corp-sales"
  /family/       → DT_MOUNT → zone "family"
  /family/work/  → DT_MOUNT → zone "corp"       (cross-link)

Run (from inside Docker network):
    docker compose -f dockerfiles/docker-compose.dynamic-federation-test.yml up -d
    docker compose -f dockerfiles/docker-compose.dynamic-federation-test.yml logs -f test
"""

import re
import time
import uuid

import httpx
import pytest

# All tests share one Docker cluster — run sequentially.
pytestmark = [pytest.mark.xdist_group("dynamic-federation-e2e")]

# ---------------------------------------------------------------------------
# Configuration — Docker-internal addresses
# ---------------------------------------------------------------------------
NODE1_URL = "http://nexus-1:2026"
NODE2_URL = "http://nexus-2:2026"
HEALTH_TIMEOUT = 120

_NODE_ID_TO_URL: dict[int, str] = {1: NODE1_URL, 2: NODE2_URL}
_LEADER_HINT_RE = re.compile(r"leader hint: Some\((\d+)\)")

E2E_ADMIN_API_KEY = "sk-test-dynamic-federation-key"


# ---------------------------------------------------------------------------
# Helpers (shared with test_federation_e2e.py — keep in sync)
# ---------------------------------------------------------------------------
def _jsonrpc(url: str, method: str, params: dict, *, api_key: str, timeout: float = 15) -> dict:
    """Send a JSON-RPC request, following Raft leader hints."""
    current_url = url
    for _attempt in range(3):
        resp = httpx.post(
            f"{current_url}/api/nfs/{method}",
            json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            trust_env=False,
        )
        result = resp.json()
        error = result.get("error")
        if error and "not leader" in str(error.get("message", "")):
            match = _LEADER_HINT_RE.search(str(error["message"]))
            if match:
                leader_id = int(match.group(1))
                leader_url = _NODE_ID_TO_URL.get(leader_id)
                if leader_url and leader_url != current_url:
                    current_url = leader_url
                    continue
        return result
    return result


def _health(url: str) -> dict | None:
    try:
        resp = httpx.get(f"{url}/health", timeout=5, trust_env=False)
        if resp.status_code == 200:
            return resp.json()
    except httpx.TransportError:
        pass
    return None


def _wait_healthy(urls: list[str], timeout: float = HEALTH_TIMEOUT) -> None:
    deadline = time.time() + timeout
    for url in urls:
        while time.time() < deadline:
            h = _health(url)
            if h and h.get("status") == "healthy":
                break
            time.sleep(2)
        else:
            pytest.fail(f"Timed out waiting for {url} to become healthy")


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _wait_replicated(
    url: str,
    parent: str,
    expected_path: str,
    api_key: str,
    *,
    msg: str = "Not replicated",
    timeout: float = 15,
) -> None:
    """Poll list on a node until expected_path appears."""
    deadline = time.time() + timeout
    while True:
        ls = _jsonrpc(url, "list", {"path": parent}, api_key=api_key, timeout=5)
        if "error" not in ls:
            files = ls.get("result", {})
            if isinstance(files, dict):
                files = files.get("files", [])
            paths = [f["path"] if isinstance(f, dict) else f for f in files]
            if expected_path in paths:
                return
        if time.time() >= deadline:
            pytest.fail(f"{msg}: {expected_path} not in {parent} on {url}")
        time.sleep(0.5)


def _wait_zone_ready(
    url: str,
    zone_id: str,
    api_key: str,
    *,
    timeout: float = 30,
) -> None:
    """Wait until a zone is visible via federation_list_zones on a node."""
    deadline = time.time() + timeout
    while True:
        r = _jsonrpc(url, "federation_list_zones", {}, api_key=api_key, timeout=5)
        if "error" not in r:
            zones = r.get("result", {}).get("zones", [])
            zone_ids = [z["zone_id"] for z in zones]
            if zone_id in zone_ids:
                return
        if time.time() >= deadline:
            pytest.fail(f"Zone '{zone_id}' not ready on {url} within {timeout}s")
        time.sleep(1)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def cluster():
    """Wait for the dynamic-federation cluster to become healthy."""
    _wait_healthy([NODE1_URL, NODE2_URL])
    return {"node1": NODE1_URL, "node2": NODE2_URL}


@pytest.fixture(scope="module")
def api_key(cluster):
    return E2E_ADMIN_API_KEY


# ---------------------------------------------------------------------------
# Class 1: Bare Cluster Verification
# ---------------------------------------------------------------------------
class TestBareClusterHealth:
    """Verify nodes start with only root zone — no pre-created topology."""

    def test_both_nodes_healthy(self, cluster):
        for url in [cluster["node1"], cluster["node2"]]:
            h = _health(url)
            assert h is not None
            assert h["status"] == "healthy"

    def test_only_root_zone_exists(self, cluster, api_key):
        """Nodes should have only the root zone after clean startup."""
        r = _jsonrpc(cluster["node1"], "federation_list_zones", {}, api_key=api_key)
        assert "error" not in r, f"federation_list_zones failed: {r}"
        zones = r["result"]["zones"]
        zone_ids = [z["zone_id"] for z in zones]
        assert "root" in zone_ids, f"Root zone missing: {zone_ids}"
        # No pre-created zones — just root
        assert len(zone_ids) == 1, f"Expected only root zone, got: {zone_ids}"

    def test_root_zone_write_read(self, cluster, api_key):
        """Root zone should be functional for basic file operations."""
        uid = _uid()
        path = f"/workspace/bare-{uid}.txt"
        w = _jsonrpc(
            cluster["node1"],
            "write",
            {"path": path, "content": f"bare-{uid}"},
            api_key=api_key,
        )
        assert "error" not in w, f"Root write failed: {w}"
        r = _jsonrpc(cluster["node1"], "read", {"path": path}, api_key=api_key)
        assert "error" not in r, f"Root read failed: {r}"


# ---------------------------------------------------------------------------
# Class 2: Zone Creation + Node-2 Join (Explicit API)
# ---------------------------------------------------------------------------
class TestDynamicZoneCreation:
    """Create zones on node-1, join from node-2 via explicit API."""

    def test_create_zones_on_node1(self, cluster, api_key):
        """Node-1: create zones with path binding (create_zone = share_subtree)."""
        node = cluster["node1"]

        # mkdir first, then create_zone (which does share_subtree internally)
        for path, zone_id in [
            ("/corp", "corp"),
            ("/family", "family"),
        ]:
            mk = _jsonrpc(node, "mkdir", {"path": path, "parents": True}, api_key=api_key)
            assert "error" not in mk, f"mkdir {path} failed: {mk}"

            r = _jsonrpc(
                node,
                "federation_create_zone",
                {"path": path, "zone_id": zone_id},
                api_key=api_key,
            )
            assert "error" not in r, f"create_zone({zone_id}) failed: {r}"
            assert r["result"]["created"] is True

    def test_create_nested_zones_on_node1(self, cluster, api_key):
        """Node-1: create nested zones (corp-eng, corp-sales under /corp)."""
        node = cluster["node1"]

        for path, zone_id in [
            ("/corp/eng", "corp-eng"),
            ("/corp/sales", "corp-sales"),
        ]:
            mk = _jsonrpc(node, "mkdir", {"path": path, "parents": True}, api_key=api_key)
            assert "error" not in mk, f"mkdir {path} failed: {mk}"

            r = _jsonrpc(
                node,
                "federation_create_zone",
                {"path": path, "zone_id": zone_id},
                api_key=api_key,
            )
            assert "error" not in r, f"create_zone({zone_id}) failed: {r}"

    def test_all_zones_visible_on_node1(self, cluster, api_key):
        """All 5 zones should be visible on node-1."""
        r = _jsonrpc(cluster["node1"], "federation_list_zones", {}, api_key=api_key)
        assert "error" not in r
        zone_ids = sorted(z["zone_id"] for z in r["result"]["zones"])
        expected = sorted(["root", "corp", "corp-eng", "corp-sales", "family"])
        assert zone_ids == expected, f"Expected {expected}, got {zone_ids}"

    def test_node2_joins_zones(self, cluster, api_key):
        """Node-2: join all zones from node-1 via federation_join_zone."""
        node2 = cluster["node2"]

        for path, zone_id in [
            ("/corp", "corp"),
            ("/corp/eng", "corp-eng"),
            ("/corp/sales", "corp-sales"),
            ("/family", "family"),
        ]:
            r = _jsonrpc(
                node2,
                "federation_join_zone",
                {"path": path, "zone_id": zone_id, "peer": "nexus-1:2126"},
                api_key=api_key,
            )
            assert "error" not in r, f"join_zone({zone_id}) on node-2 failed: {r}"
            assert r["result"].get("joined") or r["result"].get("cross_link"), (
                f"join_zone({zone_id}) unexpected result: {r}"
            )

    def test_zones_visible_on_node2(self, cluster, api_key):
        """All zones should be visible on node-2 after joining."""
        for zone_id in ["corp", "corp-eng", "corp-sales", "family"]:
            _wait_zone_ready(cluster["node2"], zone_id, api_key, timeout=30)


# ---------------------------------------------------------------------------
# Class 3: Cross-Link via join_zone (local-to-local)
# ---------------------------------------------------------------------------
class TestDynamicMountTopology:
    """Cross-link: mount same zone at additional paths."""

    def test_crosslink_corp_at_family_work(self, cluster, api_key):
        """Cross-link: mount corp zone at /family/work on node-1."""
        node = cluster["node1"]

        mk = _jsonrpc(node, "mkdir", {"path": "/family/work", "parents": True}, api_key=api_key)
        assert "error" not in mk, f"mkdir /family/work failed: {mk}"

        r = _jsonrpc(
            node,
            "federation_join_zone",
            {"path": "/family/work", "zone_id": "corp"},
            api_key=api_key,
        )
        assert "error" not in r, f"mount cross-link failed: {r}"


# ---------------------------------------------------------------------------
# Class 4: Cross-Zone File Operations (Dynamic Topology)
# ---------------------------------------------------------------------------
class TestDynamicCrossZoneOps:
    """Verify file ops work through dynamically created mount topology."""

    def test_write_read_through_mount(self, cluster, api_key):
        """Write/read through DT_MOUNT (root → corp)."""
        uid = _uid()
        path = f"/corp/dyn-{uid}.txt"
        node = cluster["node1"]

        w = _jsonrpc(node, "write", {"path": path, "content": f"dynamic-{uid}"}, api_key=api_key)
        assert "error" not in w, f"Write through mount failed: {w}"

        r = _jsonrpc(node, "read", {"path": path}, api_key=api_key)
        assert "error" not in r, f"Read through mount failed: {r}"

    def test_nested_mount_write_read(self, cluster, api_key):
        """Write/read through nested mount (root → corp → corp-eng)."""
        uid = _uid()
        path = f"/corp/eng/nested-{uid}.py"
        node = cluster["node1"]

        w = _jsonrpc(
            node,
            "write",
            {"path": path, "content": f"def nested(): pass  # {uid}"},
            api_key=api_key,
        )
        assert "error" not in w, f"Nested write failed: {w}"

        r = _jsonrpc(node, "read", {"path": path}, api_key=api_key)
        assert "error" not in r, f"Nested read failed: {r}"

    def test_crosslink_read(self, cluster, api_key):
        """Write via /corp, read via /family/work (cross-link, same zone)."""
        uid = _uid()
        node = cluster["node1"]

        # Write via /corp path
        corp_path = f"/corp/crosslink-{uid}.md"
        w = _jsonrpc(
            node,
            "write",
            {"path": corp_path, "content": f"cross-{uid}"},
            api_key=api_key,
        )
        assert "error" not in w

        # Read via cross-link /family/work path
        crosslink_path = f"/family/work/crosslink-{uid}.md"
        r = _jsonrpc(node, "read", {"path": crosslink_path}, api_key=api_key)
        assert "error" not in r, f"Cross-link read failed: {r}"

    def test_family_zone_isolation(self, cluster, api_key):
        """Family-only files not visible in corp zone."""
        uid = _uid()
        node = cluster["node1"]

        # Write to family zone
        family_path = f"/family/private-{uid}.txt"
        w = _jsonrpc(
            node,
            "write",
            {"path": family_path, "content": f"private-{uid}"},
            api_key=api_key,
        )
        assert "error" not in w

        # Corp listing should NOT contain family file
        ls = _jsonrpc(node, "list", {"path": "/corp/"}, api_key=api_key)
        assert "error" not in ls
        files = ls.get("result", {})
        if isinstance(files, dict):
            files = files.get("files", [])
        paths = [f["path"] if isinstance(f, dict) else f for f in files]
        assert family_path not in paths, "Family file leaked into corp zone!"


# ---------------------------------------------------------------------------
# Class 5: Cross-Node Replication (Dynamic Topology)
# ---------------------------------------------------------------------------
class TestDynamicCrossNodeReplication:
    """Write on node-1 through dynamic mount, verify on node-2."""

    def test_cross_zone_replication(self, cluster, api_key):
        uid = _uid()

        # Write to corp-eng on node-1
        eng_path = f"/corp/eng/replicated-{uid}.txt"
        w = _jsonrpc(
            cluster["node1"],
            "write",
            {"path": eng_path, "content": f"repl-{uid}"},
            api_key=api_key,
        )
        assert "error" not in w

        # Verify on node-2
        _wait_replicated(
            cluster["node2"],
            "/corp/eng/",
            eng_path,
            api_key,
            msg="corp-eng file not replicated to node-2",
        )

    def test_family_zone_replication(self, cluster, api_key):
        uid = _uid()

        family_path = f"/family/repl-{uid}.txt"
        w = _jsonrpc(
            cluster["node1"],
            "write",
            {"path": family_path, "content": f"family-repl-{uid}"},
            api_key=api_key,
        )
        assert "error" not in w

        _wait_replicated(
            cluster["node2"],
            "/family/",
            family_path,
            api_key,
            msg="Family file not replicated to node-2",
        )


# ---------------------------------------------------------------------------
# Class 6: Dynamic Unmount and Remount
# ---------------------------------------------------------------------------
class TestDynamicUnmountRemount:
    """Unmount a zone, verify inaccessible, remount, verify accessible."""

    def test_unmount_remount_cycle(self, cluster, api_key):
        uid = _uid()
        node = cluster["node1"]

        # Write a file in corp-sales
        path = f"/corp/sales/unmount-test-{uid}.txt"
        w = _jsonrpc(node, "write", {"path": path, "content": f"before-{uid}"}, api_key=api_key)
        assert "error" not in w

        # Unmount corp-sales (new API: just path)
        um = _jsonrpc(
            node,
            "federation_unmount",
            {"path": "/corp/sales"},
            api_key=api_key,
        )
        assert "error" not in um, f"Unmount failed: {um}"

        # File should be inaccessible through mount path
        r = _jsonrpc(node, "read", {"path": path}, api_key=api_key)
        assert "error" in r, "File should be inaccessible after unmount"

        # Remount via join_zone (zone already exists locally, this is a re-link)
        rm = _jsonrpc(
            node,
            "federation_join_zone",
            {"path": "/corp/sales", "zone_id": "corp-sales"},
            api_key=api_key,
        )
        assert "error" not in rm, f"Remount failed: {rm}"

        # File should be accessible again
        r2 = _jsonrpc(node, "read", {"path": path}, api_key=api_key)
        assert "error" not in r2, f"File not accessible after remount: {r2}"


# ---------------------------------------------------------------------------
# Class 7: Zone Info and Cluster State
# ---------------------------------------------------------------------------
class TestZoneInfoAndState:
    """Verify cluster info API reflects dynamic topology state."""

    def test_cluster_info_per_zone(self, cluster, api_key):
        """federation_cluster_info should return valid info for each zone."""
        for zone_id in ["root", "corp", "corp-eng", "corp-sales", "family"]:
            r = _jsonrpc(
                cluster["node1"],
                "federation_cluster_info",
                {"zone_id": zone_id},
                api_key=api_key,
            )
            assert "error" not in r, f"cluster_info({zone_id}) failed: {r}"
            info = r["result"]
            assert info["zone_id"] == zone_id
            assert info["has_store"] is True

    def test_links_count_reflects_mounts(self, cluster, api_key):
        """Corp zone should have links_count >= 2 (mounted at /corp and /family/work)."""
        r = _jsonrpc(
            cluster["node1"],
            "federation_cluster_info",
            {"zone_id": "corp"},
            api_key=api_key,
        )
        assert "error" not in r
        assert r["result"]["links_count"] >= 2, (
            f"Corp zone links_count should be >= 2 (dual mount), got {r['result']['links_count']}"
        )
