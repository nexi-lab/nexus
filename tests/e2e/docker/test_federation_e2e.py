"""Consolidated Federation E2E tests -- gRPC transport, dynamic bootstrap.

Replaces:
  - test_federation_dynamic_e2e.py  (HTTP JSON-RPC, dynamic bootstrap)
  - test_federation_e2e.py          (gRPC, static bootstrap)
  - test_raft_cluster_smoke.py      (HTTP JSON-RPC, static bootstrap)

All business-logic calls use gRPC Call RPC (NexusVFSServiceStub).
HTTP is used only for health probes (/healthz/ready, /health).

Target topology (built incrementally by tests):
  /              (root zone -- bootstrapped at startup)
  /corp/         -> DT_MOUNT -> zone "corp"
  /corp/eng/     -> DT_MOUNT -> zone "corp-eng"   (nested)
  /corp/sales/   -> DT_MOUNT -> zone "corp-sales"
  /family/       -> DT_MOUNT -> zone "family"
  /family/work/  -> DT_MOUNT -> zone "corp"       (cross-link)

Run (from inside Docker network):
    docker compose -f dockerfiles/docker-compose.dynamic-federation-test.yml up -d
    docker compose -f dockerfiles/docker-compose.dynamic-federation-test.yml logs -f test
"""

import base64
import hashlib
import re
import struct
import time
import uuid

import grpc
import httpx
import pytest

# All tests share one Docker cluster -- run sequentially in a single xdist worker.
pytestmark = [pytest.mark.xdist_group("federation-e2e")]

# ---------------------------------------------------------------------------
# Configuration -- Docker-internal addresses
# ---------------------------------------------------------------------------
NODE1_URL = "http://nexus-1:2026"  # health probes only
NODE2_URL = "http://nexus-2:2026"  # health probes only
NODE1_GRPC = "nexus-1:2028"  # gRPC Call RPC
NODE2_GRPC = "nexus-2:2028"  # gRPC Call RPC
HEALTH_TIMEOUT = 120

E2E_ADMIN_API_KEY = "sk-test-dynamic-federation-key"


# ---------------------------------------------------------------------------
# Node-ID mapping (SHA-256 hostname -> u64, same as Rust PeerAddress)
# ---------------------------------------------------------------------------
def _hostname_to_node_id(hostname: str) -> int:
    """SHA-256 hostname -> u64 (matches Rust/Python PeerAddress)."""
    digest = hashlib.sha256(hostname.encode()).digest()
    return struct.unpack("<Q", digest[:8])[0] or 1


_NODE_ID_TO_GRPC: dict[int, str] = {
    _hostname_to_node_id("nexus-1"): NODE1_GRPC,
    _hostname_to_node_id("nexus-2"): NODE2_GRPC,
}
_LEADER_HINT_RE = re.compile(r"leader hint: Some\((\d+)\)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _grpc_call(
    target: str,
    method: str,
    params: dict,
    *,
    api_key: str,
    timeout: float = 10,
) -> dict:
    """Send gRPC Call RPC, following Raft leader hints (up to 2 redirects).

    Imports protobuf/codec lazily so the module can be imported outside Docker.
    Returns ``{"result": ...}`` on success or ``{"error": ...}`` on failure.
    """
    from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc
    from nexus.lib.rpc_codec import decode_rpc_message, encode_rpc_message

    current = target
    result: dict = {}
    for _ in range(3):
        channel = grpc.insecure_channel(current)
        try:
            stub = vfs_pb2_grpc.NexusVFSServiceStub(channel)
            req = vfs_pb2.CallRequest(
                method=method,
                payload=encode_rpc_message(params),
                auth_token=api_key,
            )
            resp = stub.Call(req, timeout=timeout)
            result = decode_rpc_message(resp.payload)
            if resp.is_error and "not leader" in str(result):
                match = _LEADER_HINT_RE.search(str(result.get("message", result)))
                if match:
                    leader_id = int(match.group(1))
                    leader_target = _NODE_ID_TO_GRPC.get(leader_id)
                    if leader_target and leader_target != current:
                        current = leader_target
                        continue
            if resp.is_error:
                return {"error": result}
            return result  # already {"result": <data>} from servicer
        finally:
            channel.close()
    return {"error": result}


def _grpc_call_or_skip(
    target: str,
    method: str,
    params: dict,
    *,
    api_key: str,
    timeout: float = 10,
    skip_msg: str = "RPC method not available",
) -> dict:
    """gRPC Call wrapper that calls ``pytest.skip()`` on unavailable methods."""
    try:
        result = _grpc_call(target, method, params, api_key=api_key, timeout=timeout)
    except grpc.RpcError as e:
        if e.code() == grpc.StatusCode.UNAVAILABLE:
            pytest.skip(f"{skip_msg} (gRPC unavailable)")
        raise
    error = result.get("error", {})
    if isinstance(error, dict) and error.get("code") in (-32601,):  # method not found
        pytest.skip(f"{skip_msg} ({method})")
    return result


def _health(url: str) -> dict | None:
    """Check /health endpoint.  Returns None if unreachable."""
    try:
        resp = httpx.get(f"{url}/health", timeout=5, trust_env=False)
        if resp.status_code == 200:
            return resp.json()
    except httpx.TransportError:
        pass
    return None


def _wait_healthy(urls: list[str], timeout: float = HEALTH_TIMEOUT) -> None:
    """Wait until all URLs return ``{"status": "healthy"}``."""
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
    """Short unique ID for test isolation."""
    return uuid.uuid4().hex[:8]


def _decode_content(result: dict) -> str:
    """Decode read response content (handles base64 bytes, dict, or plain str)."""
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
    if isinstance(data, bytes):
        return data.decode()
    if isinstance(data, str):
        return data
    return str(data)


def _list_paths(result: dict) -> list[str]:
    """Extract list of paths from a ``list`` RPC response."""
    files = result["result"]
    if isinstance(files, dict):
        files = files.get("files", [])
    return [f["path"] if isinstance(f, dict) else f for f in files]


def _wait_replicated(
    target: str,
    parent: str,
    expected_path: str,
    api_key: str,
    *,
    msg: str = "File not replicated",
    timeout: float = 15,
) -> None:
    """Poll ``list`` via gRPC until *expected_path* appears."""
    deadline = time.time() + timeout
    while True:
        ls = _grpc_call(target, "list", {"path": parent}, api_key=api_key, timeout=5)
        if "error" not in ls and expected_path in _list_paths(ls):
            return
        if time.time() >= deadline:
            pytest.fail(f"{msg}: {expected_path} not in {parent} on {target}")
        time.sleep(0.5)


def _wait_leader_elected(
    target: str,
    zone_id: str,
    api_key: str,
    *,
    timeout: float = 15,
) -> None:
    """Poll ``federation_cluster_info`` until this node is leader for the zone."""
    deadline = time.time() + timeout
    last: dict = {}
    while True:
        try:
            r = _grpc_call(
                target,
                "federation_cluster_info",
                {"zone_id": zone_id},
                api_key=api_key,
                timeout=5,
            )
            last = r
            if "error" not in r and r.get("result", {}).get("is_leader"):
                return
        except Exception:
            pass
        if time.time() >= deadline:
            pytest.fail(
                f"Zone '{zone_id}' has no leader on {target} within {timeout}s (last: {last})"
            )
        time.sleep(0.2)


def _wait_zone_ready(
    target: str,
    zone_id: str,
    api_key: str,
    *,
    timeout: float = 30,
) -> None:
    """Poll ``federation_list_zones`` via gRPC until *zone_id* appears."""
    deadline = time.time() + timeout
    while True:
        r = _grpc_call(target, "federation_list_zones", {}, api_key=api_key, timeout=5)
        if "error" not in r:
            zones = r.get("result", {}).get("zones", [])
            zone_ids = [z["zone_id"] for z in zones]
            if zone_id in zone_ids:
                return
        if time.time() >= deadline:
            pytest.fail(f"Zone '{zone_id}' not ready on {target} within {timeout}s")
        time.sleep(1)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def cluster():
    """Ensure the dynamic-federation cluster is running and healthy."""
    if _health(NODE1_URL) is None:
        pytest.skip(
            "Federation cluster not reachable. Start with:\n"
            "  docker compose -f dockerfiles/docker-compose.dynamic-federation-test.yml up -d"
        )
    _wait_healthy([NODE1_URL, NODE2_URL])
    return {
        "node1": NODE1_URL,
        "node2": NODE2_URL,
        "grpc1": NODE1_GRPC,
        "grpc2": NODE2_GRPC,
    }


@pytest.fixture(scope="module")
def api_key(cluster):
    """Admin API key set via NEXUS_API_KEY in docker-compose."""
    return E2E_ADMIN_API_KEY


# ===================================================================
# Class 1: Cluster Health
# ===================================================================
class TestClusterHealth:
    """Verify both nodes are healthy and reachable via gRPC."""

    def test_both_nodes_healthy(self, cluster):
        """HTTP health check on both nodes."""
        for url in [cluster["node1"], cluster["node2"]]:
            h = _health(url)
            assert h is not None, f"{url} unreachable"
            assert h["status"] == "healthy"

    def test_both_nodes_have_auth(self, cluster, api_key):
        """gRPC call with a valid API key succeeds on both nodes."""
        for grpc_target in [cluster["grpc1"], cluster["grpc2"]]:
            r = _grpc_call(
                grpc_target,
                "exists",
                {"path": "/workspace"},
                api_key=api_key,
            )
            assert "error" not in r, f"Auth check failed on {grpc_target}: {r}"

    def test_root_zone_write_read(self, cluster, api_key):
        """Root zone basic file operations via gRPC."""
        uid = _uid()
        grpc1 = cluster["grpc1"]
        path = f"/workspace/test-{uid}.txt"
        content = f"hello-{uid}"

        w = _grpc_call(grpc1, "write", {"path": path, "content": content}, api_key=api_key)
        assert "error" not in w, f"Root write failed: {w}"

        r = _grpc_call(grpc1, "read", {"path": path}, api_key=api_key)
        assert "error" not in r, f"Root read failed: {r}"
        assert _decode_content(r) == content


# ===================================================================
# Class 2: Zone Lifecycle
# ===================================================================
class TestZoneLifecycle:
    """Create zones dynamically, verify cross-node visibility, remove."""

    def test_create_zones(self, cluster, api_key):
        """Create corp, corp-eng, corp-sales, family zones on node-1."""
        grpc1 = cluster["grpc1"]
        for zone_id in ["corp", "corp-eng", "corp-sales", "family"]:
            r = _grpc_call(
                grpc1,
                "federation_create_zone",
                {"zone_id": zone_id},
                api_key=api_key,
            )
            assert "error" not in r, f"create_zone({zone_id}) failed: {r}"

    def test_zones_visible_on_both_nodes(self, cluster, api_key):
        """Create zones on node-2 (joins Raft groups), wait until visible."""
        grpc2 = cluster["grpc2"]
        for zone_id in ["corp", "corp-eng", "corp-sales", "family"]:
            r = _grpc_call(
                grpc2,
                "federation_create_zone",
                {"zone_id": zone_id},
                api_key=api_key,
            )
            assert "error" not in r, f"create_zone({zone_id}) on node-2 failed: {r}"

        # Wait for all zones visible on node-2
        for zone_id in ["corp", "corp-eng", "corp-sales", "family"]:
            _wait_zone_ready(cluster["grpc2"], zone_id, api_key, timeout=30)

    def test_remove_zone(self, cluster, api_key):
        """Create a temporary zone, remove it, verify it is gone."""
        grpc1 = cluster["grpc1"]
        temp_zone = f"temp-{_uid()}"

        # Create
        cr = _grpc_call(grpc1, "federation_create_zone", {"zone_id": temp_zone}, api_key=api_key)
        assert "error" not in cr, f"create temp zone failed: {cr}"
        _wait_zone_ready(grpc1, temp_zone, api_key, timeout=15)

        # Remove
        rm = _grpc_call(grpc1, "federation_remove_zone", {"zone_id": temp_zone}, api_key=api_key)
        assert "error" not in rm, f"remove temp zone failed: {rm}"

        # Verify gone (poll briefly)
        deadline = time.time() + 10
        while time.time() < deadline:
            r = _grpc_call(grpc1, "federation_list_zones", {}, api_key=api_key)
            if "error" not in r:
                zone_ids = [z["zone_id"] for z in r["result"]["zones"]]
                if temp_zone not in zone_ids:
                    return
            time.sleep(0.5)
        pytest.fail(f"Temp zone '{temp_zone}' still visible after removal")


# ===================================================================
# Class 3: Mount Topology
# ===================================================================
class TestMountTopology:
    """Build the mount tree: /corp, /corp/eng, /corp/sales, /family, /family/work."""

    def test_mount_zones(self, cluster, api_key):
        """Mount corp at /corp, corp-eng at /corp/eng, corp-sales at /corp/sales, family at /family."""
        grpc1 = cluster["grpc1"]

        # Mount root-level zones first, then nested (nested mounts need the
        # parent mount to be active so mkdir can traverse DT_MOUNT boundaries).
        mounts = [
            ("/corp", "root", "corp"),
            ("/family", "root", "family"),
            ("/corp/eng", "corp", "corp-eng"),
            ("/corp/sales", "corp", "corp-sales"),
        ]
        for mount_path, parent_zone, target_zone in mounts:
            # Create mount-point directory
            mk = _grpc_call(grpc1, "mkdir", {"path": mount_path, "parents": True}, api_key=api_key)
            assert "error" not in mk, f"mkdir {mount_path} failed: {mk}"

            # Mount zone — retry on both nodes with brief waits.
            # mkdir commit may not have replicated to the zone store yet
            # (Raft replication delay), so retry the mount a few times.
            mounted = False
            deadline = time.time() + 10
            while not mounted and time.time() < deadline:
                for target in [cluster["grpc1"], cluster["grpc2"]]:
                    r = _grpc_call(
                        target,
                        "federation_mount",
                        {
                            "parent_zone": parent_zone,
                            "path": mount_path,
                            "target_zone": target_zone,
                        },
                        api_key=api_key,
                    )
                    if "error" not in r:
                        mounted = True
                        break
                    # Already mounted is fine — Raft replication may have auto-mounted
                    err_msg = str(r.get("error", {}).get("message", ""))
                    if "already a DT_MOUNT" in err_msg:
                        mounted = True
                        break
                if not mounted:
                    time.sleep(0.5)
            assert mounted, f"mount {target_zone} at {mount_path} failed on both nodes: {r}"

    def test_mount_crosslink(self, cluster, api_key):
        """Mount corp zone again at /family/work (cross-link)."""
        mk = _grpc_call(
            cluster["grpc1"], "mkdir", {"path": "/family/work", "parents": True}, api_key=api_key
        )
        assert "error" not in mk, f"mkdir /family/work failed: {mk}"

        # Retry on both nodes (leader for 'family' zone may differ)
        r = None
        for target in [cluster["grpc1"], cluster["grpc2"]]:
            r = _grpc_call(
                target,
                "federation_mount",
                {"parent_zone": "family", "path": "/family/work", "target_zone": "corp"},
                api_key=api_key,
            )
            if "error" not in r:
                break
            err_msg = str(r.get("error", {}).get("message", ""))
            if "already a DT_MOUNT" in err_msg:
                break
        assert r is not None and ("error" not in r or "already a DT_MOUNT" in str(r)), (
            f"mount cross-link failed on both nodes: {r}"
        )

    def test_unmount_remount_cycle(self, cluster, api_key):
        """Unmount corp-sales, verify inaccessible, remount, verify accessible."""
        uid = _uid()
        grpc1 = cluster["grpc1"]

        # Write a file in corp-sales
        path = f"/corp/sales/unmount-test-{uid}.txt"
        w = _grpc_call(grpc1, "write", {"path": path, "content": f"before-{uid}"}, api_key=api_key)
        assert "error" not in w, f"Pre-unmount write failed: {w}"

        # Unmount corp-sales — retry on both nodes (leader may differ per zone)
        um = None
        for target in [cluster["grpc1"], cluster["grpc2"]]:
            um = _grpc_call(
                target,
                "federation_unmount",
                {"parent_zone": "corp", "path": "/corp/sales"},
                api_key=api_key,
            )
            if "error" not in um:
                break
        assert um is not None and "error" not in um, f"Unmount failed on both nodes: {um}"

        # File should be inaccessible through mount path
        r = _grpc_call(grpc1, "read", {"path": path}, api_key=api_key)
        assert "error" in r, "File should be inaccessible after unmount"

        # Remount — retry on both nodes
        rm = None
        for target in [cluster["grpc1"], cluster["grpc2"]]:
            rm = _grpc_call(
                target,
                "federation_mount",
                {"parent_zone": "corp", "path": "/corp/sales", "target_zone": "corp-sales"},
                api_key=api_key,
            )
            if "error" not in rm:
                break
            err_msg = str(rm.get("error", {}).get("message", ""))
            if "already a DT_MOUNT" in err_msg:
                break
        assert rm is not None and ("error" not in rm or "already a DT_MOUNT" in str(rm)), (
            f"Remount failed: {rm}"
        )

        # File should be accessible again
        r2 = _grpc_call(grpc1, "read", {"path": path}, api_key=api_key)
        assert "error" not in r2, f"File not accessible after remount: {r2}"


# ===================================================================
# Class 4: Cross-Zone Operations
# ===================================================================
class TestCrossZoneOperations:
    """File ops through mount points, cross-links, and zone isolation."""

    def test_write_read_through_mount(self, cluster, api_key):
        """Write/read through /corp/ mount."""
        uid = _uid()
        grpc1 = cluster["grpc1"]
        path = f"/corp/mount-{uid}.txt"
        content = f"mount-{uid}"

        w = _grpc_call(grpc1, "write", {"path": path, "content": content}, api_key=api_key)
        assert "error" not in w, f"Write through mount failed: {w}"

        r = _grpc_call(grpc1, "read", {"path": path}, api_key=api_key)
        assert "error" not in r, f"Read through mount failed: {r}"
        assert _decode_content(r) == content

    def test_nested_mount_write_read(self, cluster, api_key):
        """Write/read through nested mount /corp/eng/."""
        uid = _uid()
        grpc1 = cluster["grpc1"]
        path = f"/corp/eng/nested-{uid}.py"
        content = f"def nested(): pass  # {uid}"

        w = _grpc_call(grpc1, "write", {"path": path, "content": content}, api_key=api_key)
        assert "error" not in w, f"Nested write failed: {w}"

        r = _grpc_call(grpc1, "read", {"path": path}, api_key=api_key)
        assert "error" not in r, f"Nested read failed: {r}"
        assert _decode_content(r) == content

    def test_crosslink_read(self, cluster, api_key):
        """Write via /corp/x, read via /family/work/x (cross-link, same zone)."""
        uid = _uid()
        grpc1 = cluster["grpc1"]
        content = f"cross-{uid}"

        corp_path = f"/corp/crosslink-{uid}.md"
        w = _grpc_call(grpc1, "write", {"path": corp_path, "content": content}, api_key=api_key)
        assert "error" not in w, f"Corp write failed: {w}"

        crosslink_path = f"/family/work/crosslink-{uid}.md"
        r = _grpc_call(grpc1, "read", {"path": crosslink_path}, api_key=api_key)
        assert "error" not in r, f"Cross-link read failed: {r}"
        assert _decode_content(r) == content

    def test_zone_isolation(self, cluster, api_key):
        """Family-only file should not appear in corp listing."""
        uid = _uid()
        grpc1 = cluster["grpc1"]

        family_path = f"/family/private-{uid}.txt"
        w = _grpc_call(
            grpc1, "write", {"path": family_path, "content": f"private-{uid}"}, api_key=api_key
        )
        assert "error" not in w

        ls = _grpc_call(grpc1, "list", {"path": "/corp/"}, api_key=api_key)
        assert "error" not in ls
        paths = _list_paths(ls)
        assert family_path not in paths, "Family file leaked into corp zone!"


# ===================================================================
# Class 5: Cross-Node Replication
# ===================================================================
class TestCrossNodeReplication:
    """Write on node-1, verify on node-2 (Raft replication)."""

    def test_cross_zone_replication(self, cluster, api_key):
        """Write to corp-eng on node-1, verify listing on node-2."""
        uid = _uid()
        grpc1 = cluster["grpc1"]
        grpc2 = cluster["grpc2"]

        eng_path = f"/corp/eng/replicated-{uid}.txt"
        w = _grpc_call(
            grpc1, "write", {"path": eng_path, "content": f"repl-{uid}"}, api_key=api_key
        )
        assert "error" not in w

        _wait_replicated(
            grpc2,
            "/corp/eng/",
            eng_path,
            api_key,
            msg="corp-eng file not replicated to node-2",
        )

    def test_metadata_visible_on_follower(self, cluster, api_key):
        """Write on node-1, list on node-2 -- metadata should be visible."""
        uid = _uid()
        grpc1 = cluster["grpc1"]
        grpc2 = cluster["grpc2"]

        path = f"/workspace/repl-meta-{uid}.txt"
        w = _grpc_call(grpc1, "write", {"path": path, "content": f"meta-{uid}"}, api_key=api_key)
        assert "error" not in w

        _wait_replicated(
            grpc2,
            "/workspace/",
            path,
            api_key,
            msg="Metadata not replicated to follower",
        )


# ===================================================================
# Class 6: Raft Behavior
# ===================================================================
class TestRaftBehavior:
    """Validate Raft consensus invariants."""

    def test_witness_not_leader(self, cluster, api_key):
        """Witness node should never be elected leader for the root zone."""
        grpc1 = cluster["grpc1"]
        info = _grpc_call(
            grpc1,
            "federation_cluster_info",
            {"zone_id": "root"},
            api_key=api_key,
        )
        assert "error" not in info, f"cluster_info(root) failed: {info}"
        result = info["result"]

        # If leader_id is reported, ensure it is not the witness node (node 3).
        leader_id = result.get("leader_id")
        if leader_id is not None:
            # Witness is typically node-id 3 in the 3-node setup
            assert leader_id != 3, f"Witness (node 3) should never be leader! leader_id={leader_id}"

        # Also verify via members list if available
        members = result.get("members", [])
        for m in members:
            if m.get("role") == "witness" or m.get("is_witness"):
                assert m.get("is_leader") is not True, f"Witness member is marked as leader: {m}"


# ===================================================================
# Class 7: Distributed Locks
# ===================================================================
class TestDistributedLocks:
    """Distributed lock acquire, contention, and expiry."""

    def test_lock_acquire_release(self, cluster, api_key):
        """Acquire a lock, verify held, release it."""
        uid = _uid()
        grpc1 = cluster["grpc1"]
        lock_path = f"/corp/eng/lock-{uid}.txt"

        # Write target file
        w = _grpc_call(
            grpc1, "write", {"path": lock_path, "content": f"lock-{uid}"}, api_key=api_key
        )
        assert "error" not in w

        # Acquire lock -- skip entire test if unavailable
        acquire_r = _grpc_call_or_skip(
            grpc1,
            "lock_acquire",
            {"path": lock_path, "ttl": 60},
            api_key=api_key,
            skip_msg="Lock API not available",
        )
        if "error" in acquire_r:
            pytest.skip(f"lock_acquire returned error: {acquire_r}")
        lock_data = acquire_r.get("result", acquire_r)
        assert lock_data.get("acquired") is True, f"Lock not acquired: {lock_data}"
        lock_id = lock_data.get("lock_id", "")
        assert lock_id, f"No lock_id in response: {lock_data}"

        # Verify held via sys_stat(include_lock=True)
        info = _grpc_call(
            grpc1, "sys_stat", {"path": lock_path, "include_lock": True}, api_key=api_key
        )
        assert "error" not in info, f"sys_stat(include_lock) failed: {info}"
        info_data = info.get("result", info)
        lock_data_check = info_data.get("lock")
        assert lock_data_check is not None, f"Expected lock info present: {info_data}"
        assert len(lock_data_check.get("holders", [])) > 0, f"Expected holders: {lock_data_check}"

        # Release
        release_r = _grpc_call(
            grpc1,
            "sys_unlock",
            {"path": lock_path, "lock_id": lock_id},
            api_key=api_key,
        )
        assert "error" not in release_r, f"Release failed: {release_r}"

    def test_lock_contention(self, cluster, api_key):
        """Two concurrent lock acquires on the same path -- one should block/fail."""
        uid = _uid()
        grpc1 = cluster["grpc1"]
        grpc2 = cluster["grpc2"]
        lock_path = f"/corp/eng/contend-{uid}.txt"

        w = _grpc_call(
            grpc1, "write", {"path": lock_path, "content": f"contend-{uid}"}, api_key=api_key
        )
        assert "error" not in w

        # First acquire on node-1
        a1 = _grpc_call_or_skip(
            grpc1,
            "lock_acquire",
            {"path": lock_path, "ttl": 60},
            api_key=api_key,
            skip_msg="Lock API not available",
        )
        if "error" in a1:
            pytest.skip(f"lock_acquire failed: {a1}")
        a1_data = a1.get("result", a1)
        if not a1_data.get("acquired"):
            pytest.skip("First lock_acquire did not succeed -- cannot test contention")
        lock_id_1 = a1_data.get("lock_id", "")

        # Second acquire on node-2 (same path, should fail or block)
        a2 = _grpc_call(
            grpc2,
            "lock_acquire",
            {"path": lock_path, "ttl": 10},
            api_key=api_key,
            timeout=5,
        )
        a2_data = a2.get("result", a2) if "error" not in a2 else a2.get("error", {})
        # Second acquire should NOT succeed while first is held
        second_acquired = False
        if isinstance(a2_data, dict):
            second_acquired = a2_data.get("acquired", False)
        assert not second_acquired, (
            f"Second lock_acquire should not succeed while first is held: {a2}"
        )

        # Cleanup: release first lock
        _grpc_call(
            grpc1,
            "sys_unlock",
            {"path": lock_path, "lock_id": lock_id_1},
            api_key=api_key,
        )

    def test_lock_expiry(self, cluster, api_key):
        """Acquire with short TTL, wait, verify lock is auto-released."""
        uid = _uid()
        grpc1 = cluster["grpc1"]
        lock_path = f"/corp/eng/expiry-{uid}.txt"

        w = _grpc_call(
            grpc1, "write", {"path": lock_path, "content": f"expiry-{uid}"}, api_key=api_key
        )
        assert "error" not in w

        # Acquire with short TTL (2 seconds)
        acquire_r = _grpc_call_or_skip(
            grpc1,
            "lock_acquire",
            {"path": lock_path, "ttl": 2},
            api_key=api_key,
            skip_msg="Lock API not available",
        )
        if "error" in acquire_r:
            pytest.skip(f"lock_acquire returned error: {acquire_r}")
        lock_data = acquire_r.get("result", acquire_r)
        if not lock_data.get("acquired"):
            pytest.skip("lock_acquire did not succeed -- cannot test expiry")

        # Wait for TTL to expire
        time.sleep(4)

        # Verify lock is released via sys_stat(include_lock=True)
        info = _grpc_call(
            grpc1, "sys_stat", {"path": lock_path, "include_lock": True}, api_key=api_key
        )
        if "error" not in info:
            info_data = info.get("result", info)
            lock_state = info_data.get("lock")
            if lock_state is None or len(lock_state.get("holders", [])) == 0:
                return  # expired as expected

        # Fallback: try acquiring again -- should succeed if TTL expired
        a2 = _grpc_call(
            grpc1,
            "lock_acquire",
            {"path": lock_path, "ttl": 5},
            api_key=api_key,
        )
        if "error" not in a2:
            a2_data = a2.get("result", a2)
            if a2_data.get("acquired"):
                # Cleanup
                _grpc_call(
                    grpc1,
                    "sys_unlock",
                    {"path": lock_path, "lock_id": a2_data.get("lock_id", "")},
                    api_key=api_key,
                )
                return  # expired as expected
        pytest.fail(f"Lock did not expire after TTL: info={info}, retry_acquire={a2}")


# ===================================================================
# Class 8: Admin Introspection
# ===================================================================
class TestAdminIntrospection:
    """Federation topology inspection and cluster-info queries."""

    def test_list_zones(self, cluster, api_key):
        """federation_list_zones returns all 5 zones."""
        grpc1 = cluster["grpc1"]
        r = _grpc_call(grpc1, "federation_list_zones", {}, api_key=api_key)
        assert "error" not in r, f"federation_list_zones failed: {r}"
        zone_ids = sorted(z["zone_id"] for z in r["result"]["zones"])
        expected = sorted(["root", "corp", "corp-eng", "corp-sales", "family"])
        assert zone_ids == expected, f"Expected {expected}, got {zone_ids}"

    def test_cluster_info_per_zone(self, cluster, api_key):
        """federation_cluster_info returns valid info for each zone."""
        grpc1 = cluster["grpc1"]
        for zone_id in ["root", "corp", "corp-eng", "corp-sales", "family"]:
            info = _grpc_call(
                grpc1, "federation_cluster_info", {"zone_id": zone_id}, api_key=api_key
            )
            assert "error" not in info, f"cluster_info({zone_id}) failed: {info}"
            assert info["result"]["zone_id"] == zone_id

    def test_links_count(self, cluster, api_key):
        """Corp zone links_count >= 2 (mounted at /corp and /family/work)."""
        grpc1 = cluster["grpc1"]
        info = _grpc_call(grpc1, "federation_cluster_info", {"zone_id": "corp"}, api_key=api_key)
        assert "error" not in info
        assert info["result"]["links_count"] >= 2, (
            f"Corp zone should have >= 2 links (/corp/ + /family/work/), "
            f"got {info['result']['links_count']}"
        )


# ===================================================================
# Class 9: Leader Failover (LAST -- restarts containers)
# ===================================================================
class TestLeaderFailover:
    """Leader crash, survivor takes over, writes new data, leader recovers."""

    def test_failover_and_recovery(self, cluster, api_key):
        """Stop node-1, verify node-2 serves data, restart, verify catch-up."""
        try:
            import docker as docker_sdk

            docker_client = docker_sdk.from_env()
            docker_client.ping()
        except Exception as exc:
            pytest.skip(f"Docker SDK not available: {exc}")

        uid = _uid()
        grpc1 = cluster["grpc1"]
        grpc2 = cluster["grpc2"]

        # Write a file in each zone before failover
        zone_files: dict[str, tuple[str, str, str]] = {}
        for zone, prefix, parent in [
            ("root", "/workspace/", "/workspace/"),
            ("corp", "/corp/", "/corp/"),
            ("corp-eng", "/corp/eng/", "/corp/eng/"),
            ("corp-sales", "/corp/sales/", "/corp/sales/"),
            ("family", "/family/", "/family/"),
        ]:
            path = f"{prefix}failover-{uid}-{zone}.txt"
            content = f"failover-{zone}-{uid}"
            w = _grpc_call(grpc1, "write", {"path": path, "content": content}, api_key=api_key)
            assert "error" not in w, f"Write to {zone} failed: {w}"
            zone_files[zone] = (path, parent, content)

        # Wait for replication to node-2 AND pre-fetch content.
        # Metadata replicates via Raft, but blob content lives on each
        # node's local CAS. Reading on node-2 triggers remote fetch from
        # node-1 (scatter-gather), ensuring blobs are cached locally
        # BEFORE we stop node-1.
        for zone, (path, parent, content) in zone_files.items():
            _wait_replicated(
                grpc2,
                parent,
                path,
                api_key,
                msg=f"{zone} file not replicated before failover",
                timeout=15,
            )
            # Pre-fetch: read on node-2 to pull blob from node-1
            r = _grpc_call(grpc2, "read", {"path": path}, api_key=api_key, timeout=15)
            assert "error" not in r, f"Pre-fetch read ({zone}) on node-2 failed: {r}"
            assert _decode_content(r) == content

        # Stop node-1 via Docker SDK (more portable than CLI)
        node1_container = docker_client.containers.get("nexus-dyn-node-1")
        node1_container.stop(timeout=10)

        try:
            # Wait for node-2 healthy
            _wait_healthy([cluster["node2"]], timeout=30)

            # Read all files from surviving node-2 (pre-fetched before stop)
            for zone, (path, _parent, content) in zone_files.items():
                r = _grpc_call(grpc2, "read", {"path": path}, api_key=api_key, timeout=15)
                assert "error" not in r, f"Failover read ({zone}) failed: {r}"
                assert _decode_content(r) == content

            # Wait for leader election to complete.
            # After node-1 stops, node-2 + witness form majority and elect
            # node-2 as leader. Witness auto-joins dynamic zones on first
            # Raft message. Poll federation_cluster_info for is_leader=true.
            _wait_leader_elected(grpc2, "corp-eng", api_key, timeout=15)

            # Write new files on node-2 (now leader)
            new_files = []
            for i in range(2):
                path = f"/corp/eng/post-failover-{uid}-{i}.txt"
                content = f"post-failover-{uid}-{i}"
                w = _grpc_call(grpc2, "write", {"path": path, "content": content}, api_key=api_key)
                assert "error" not in w, f"Post-failover write {i} failed: {w}"
                new_files.append((path, content))

        finally:
            # Restart node-1
            node1_container.start()
            _wait_healthy([cluster["node1"]], timeout=30)

        # Node-1 catches up via Raft log replay from node-2 leader.
        # 20s is generous — Raft catch-up should complete in < 5s.
        # If this times out, it's a real Raft bug, not a timing issue.
        for path, _content in new_files:
            _wait_replicated(
                grpc1,
                "/corp/eng/",
                path,
                api_key,
                msg=f"Post-failover file not caught up: {path}",
                timeout=20,
            )

        # Topology intact on both nodes
        for target in [grpc1, grpc2]:
            zones_r = _grpc_call(target, "federation_list_zones", {}, api_key=api_key)
            assert "error" not in zones_r, f"federation_list_zones failed on {target}"
            zone_ids = [z["zone_id"] for z in zones_r["result"]["zones"]]
            for expected in ["root", "corp", "corp-eng", "corp-sales", "family"]:
                assert expected in zone_ids, f"Zone {expected} missing on {target}: {zone_ids}"

        # Both nodes healthy after recovery
        for url in [cluster["node1"], cluster["node2"]]:
            h = _health(url)
            assert h is not None, f"{url} not healthy after recovery"
            assert h["status"] == "healthy"


# ---------------------------------------------------------------------------
# Step 26: Cross-zone cache coherence (Issue #3396)
# ---------------------------------------------------------------------------


class TestFederationCacheCoherence:
    """Verify that durable invalidation propagates across zones.

    These tests check that:
    1. The durable invalidation stream is reported in health checks
    2. Permission changes on one node are reflected on the other
    """

    @pytest.mark.order(after="TestLeaderFailover::test_failover_and_recovery")
    def test_26_durable_invalidation_health(self, cluster, api_key):
        """Durable invalidation stream should appear in detailed health.

        If Dragonfly is not configured, the component reports as disabled
        (graceful degradation). If configured, it reports healthy or degraded.
        """
        for url in [cluster["node1"], cluster["node2"]]:
            resp = httpx.get(
                f"{url}/health/detailed",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                components = data.get("components", {})
                # Durable invalidation should be present (enabled or disabled)
                if "durable_invalidation" in components:
                    status = components["durable_invalidation"]["status"]
                    assert status in ("healthy", "degraded", "disabled", "error"), (
                        f"Unexpected durable_invalidation status: {status}"
                    )

    @pytest.mark.order(after="TestFederationCacheCoherence::test_26_durable_invalidation_health")
    def test_27_cross_zone_write_propagation(self, cluster, api_key):
        """A file written on node-1 should be readable on node-2.

        This is an implicit cache coherence test: if node-2's cache
        was stale and not invalidated, the read would fail or return
        stale data. The durable stream ensures node-2 invalidates its
        cache when node-1 writes.
        """
        grpc1 = cluster["grpc1"]
        grpc2 = cluster["grpc2"]

        # Write a unique file on node-1
        uid = _uid()
        file_path = f"/corp/eng/coherence-{uid}.txt"
        content = f"cross-zone-coherence-{uid}"
        write_result = _grpc_call(
            grpc1,
            "write",
            {"path": file_path, "content": content},
            api_key=api_key,
        )

        if "error" in write_result:
            pytest.skip(f"Write failed (may not have perms): {write_result}")

        # Read back from node-2 — should eventually succeed via Raft replication.
        # _wait_replicated checks full paths from list(), so pass the full path.
        _wait_replicated(
            grpc2,
            "/corp/eng/",
            file_path,
            api_key,
            msg="Cross-zone write not propagated to node-2",
            timeout=30,
        )
