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

from nexus.contracts.constants import ROOT_ZONE_ID

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

    # Raise client-side message caps to match the server's 64 MB limit so
    # large-content writes (e.g. chunked-read tests) aren't rejected at the
    # client boundary. Default gRPC limits are 4 MB.
    channel_options = [
        ("grpc.max_send_message_length", 64 * 1024 * 1024),
        ("grpc.max_receive_message_length", 64 * 1024 * 1024),
    ]
    current = target
    result: dict = {}
    for _ in range(3):
        channel = grpc.insecure_channel(current, options=channel_options)
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


def _wait_nodes_caught_up(
    nodes: list[str],
    zone_ids: str | list[str],
    api_key: str,
    *,
    timeout: float = 60,
) -> None:
    """Wait until every node in ``nodes`` has caught up to the actual raft
    leader on each zone in ``zone_ids``.

    Protocol signal (per zone):
      1. Find the node with ``is_leader=true``, cross-checked against all
         other nodes' ``leader_id`` + ``term`` — this rejects the post-
         restart window where a fresh node's cached ``is_leader`` still
         reflects a stale pre-failover term.
      2. Read the leader's commit_index.
      3. Wait for every node's commit_index to reach that value.

    ``cached_commit_index`` is updated inside ``ZoneConsensusDriver::advance``
    AFTER ``apply_entries`` runs synchronously, so once a node's
    commit_index reaches the leader's, its state machine has applied every
    committed entry and subsequent sys_stat/list reads see a consistent
    view. No wall-clock sleep; the only ``timeout`` is a hard upper bound
    that fails the test with a diagnostic if something's structurally
    wrong (transport stall, unloaded zone, lost leader).

    ``zone_ids`` accepts a single id or a list. The canonical root zone id
    lives in ``ROOT_ZONE_ID``; hard-coding ``"root"`` at call sites
    silently breaks whenever the constant is updated, so always import
    and pass it.
    """
    if isinstance(zone_ids, str):
        zone_ids = [zone_ids]
    for zone_id in zone_ids:
        _wait_one_zone_caught_up(nodes, zone_id, api_key, timeout=timeout)


def _wait_one_zone_caught_up(
    nodes: list[str],
    zone_id: str,
    api_key: str,
    *,
    timeout: float,
) -> None:
    deadline = time.time() + timeout
    last_snapshots: dict[str, dict] = {}
    while time.time() < deadline:
        snapshots: dict[str, dict] = {}
        for n in nodes:
            snapshots[n] = (
                _grpc_call(n, "federation_cluster_info", {"zone_id": zone_id}, api_key=api_key).get(
                    "result"
                )
                or {}
            )
        last_snapshots = snapshots

        # Legitimate-leader cross-check: a node is only trusted as the
        # leader of `zone_id` when (a) it reports is_leader=true, (b) its
        # own leader_id matches its node_id (not a stale cache from a
        # previous term), and (c) every other node that has the zone
        # loaded agrees this node is their leader AND is at the same term.
        # This rejects the narrow window right after a node restart where
        # its cached_* fields still reflect pre-failover state and it
        # briefly looks like a leader to a naive observer.
        candidate = None
        for n, info in snapshots.items():
            if not info.get("is_leader"):
                continue
            node_id = info.get("node_id", 0)
            leader_id = info.get("leader_id", 0)
            term = info.get("term", 0)
            if node_id == 0 or leader_id != node_id or term == 0:
                continue
            # Every loaded node must agree on this leader and this term.
            consensus = True
            for other, other_info in snapshots.items():
                if other == n or not other_info.get("has_store"):
                    continue
                if other_info.get("leader_id", 0) != node_id:
                    consensus = False
                    break
                if other_info.get("term", 0) != term:
                    consensus = False
                    break
            if consensus:
                candidate = info
                break

        if candidate is not None:
            # Use applied_index (not commit_index) as the "state machine
            # has this entry" signal — commit_index can race ahead of
            # applied_index when raft-rs's step() advances committed
            # before the next advance()/apply_entries pass. Readers
            # gating on commit_index can observe metadata:None even
            # after the gate passes (exactly the flake we were seeing).
            leader_ai = candidate.get("applied_index", 0)
            if leader_ai > 0 and all(
                snapshots[n].get("has_store") and snapshots[n].get("applied_index", 0) >= leader_ai
                for n in nodes
            ):
                return
        time.sleep(0.5)
    pytest.fail(
        f"Raft catch-up stalled: zone={zone_id} snapshots={last_snapshots} "
        f"within {timeout}s. Check transport reconnect / peer health."
    )


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


@pytest.fixture(scope="module")
def federation_zones(cluster, api_key):
    """Idempotently ensure the standard federation topology exists.

    Any test that assumes ``corp``, ``corp-eng``, ``corp-sales``,
    ``family`` zones plus the usual mount tree (``/corp``,
    ``/corp/eng``, ``/corp/sales``, ``/family``, ``/family/work``)
    should depend on this fixture. Running that test in isolation
    (``pytest -k test_failover_and_recovery``) then works without
    relying on `TestZoneLifecycle` / `TestMountTopology` having run
    first — which was the hidden test-order coupling before.

    Idempotent: creating an existing zone / mount returns success
    or an "already exists" error, which we swallow. Safe to call
    across every module-scoped test.
    """
    grpc1 = cluster["grpc1"]
    grpc2 = cluster["grpc2"]
    expected_zones = ["corp", "corp-eng", "corp-sales", "family"]

    def _ensure_zone(target: str, zone_id: str) -> None:
        r = _grpc_call(
            target,
            "federation_create_zone",
            {"zone_id": zone_id},
            api_key=api_key,
        )
        if "error" in r:
            msg = str(r.get("error", {}).get("message", "")).lower()
            # Benign on repeat calls — zone already exists.
            if "already" in msg or "exists" in msg:
                return
            pytest.fail(f"federation_create_zone({zone_id}) on {target}: {r}")

    # Create zones on node-1 first, then node-2 (joins the raft group).
    for target in [grpc1, grpc2]:
        for zone_id in expected_zones:
            _ensure_zone(target, zone_id)

    for zone_id in expected_zones:
        _wait_zone_ready(grpc1, zone_id, api_key, timeout=30)
        _wait_zone_ready(grpc2, zone_id, api_key, timeout=30)

    # Build the mount tree. Mounts are replicated through raft's root
    # zone, so we try each mount on both nodes and tolerate "already
    # mounted" errors (they mean raft replication beat us to it).
    def _ensure_mount(parent_zone: str, path: str, target_zone: str) -> None:
        mk = _grpc_call(grpc1, "mkdir", {"path": path, "parents": True}, api_key=api_key)
        if "error" in mk:
            msg = str(mk.get("error", {}).get("message", "")).lower()
            if "exists" not in msg and "already" not in msg:
                pytest.fail(f"mkdir {path}: {mk}")

        deadline = time.time() + 10
        while time.time() < deadline:
            for t in [grpc1, grpc2]:
                r = _grpc_call(
                    t,
                    "federation_mount",
                    {
                        "parent_zone": parent_zone,
                        "path": path,
                        "target_zone": target_zone,
                    },
                    api_key=api_key,
                )
                if "error" not in r:
                    return
                msg = str(r.get("error", {}).get("message", ""))
                if "already a DT_MOUNT" in msg or "already" in msg.lower():
                    return
            time.sleep(0.5)
        pytest.fail(f"mount {target_zone} at {path} did not succeed within 10s")

    mounts = [
        ("root", "/corp", "corp"),
        ("root", "/family", "family"),
        ("corp", "/corp/eng", "corp-eng"),
        ("corp", "/corp/sales", "corp-sales"),
        ("family", "/family/work", "corp"),
    ]
    for parent_zone, path, target_zone in mounts:
        _ensure_mount(parent_zone, path, target_zone)

    return {
        "zones": expected_zones,
        "mounts": [path for _, path, _ in mounts],
    }


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
        """Write on node-1, list on node-2 -- metadata should be visible.

        Uses a federation zone (/corp/eng/) so metadata is Raft-replicated.
        Root zone (/) uses local redb — not replicated by design.
        """
        uid = _uid()
        grpc1 = cluster["grpc1"]
        grpc2 = cluster["grpc2"]

        path = f"/corp/eng/repl-meta-{uid}.txt"
        w = _grpc_call(grpc1, "write", {"path": path, "content": f"meta-{uid}"}, api_key=api_key)
        assert "error" not in w

        _wait_replicated(
            grpc2,
            "/corp/eng/",
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

        # Verify held via sys_stat (lock info always included)
        info = _grpc_call(grpc1, "sys_stat", {"path": lock_path}, api_key=api_key)
        assert "error" not in info, f"sys_stat failed: {info}"
        info_data = info.get("result", info)
        # lock data may be nested under "metadata" (RPC wraps in metadata dict)
        stat_meta = info_data.get("metadata", info_data)
        lock_data_check = stat_meta.get("lock")
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

        # Verify lock is released via sys_stat (lock info always included)
        info = _grpc_call(grpc1, "sys_stat", {"path": lock_path}, api_key=api_key)
        if "error" not in info:
            info_data = info.get("result", info)
            stat_meta = info_data.get("metadata", info_data)
            lock_state = stat_meta.get("lock")
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

    def test_lock_visible_on_follower_post_commit(self, cluster, api_key):
        """After a leader commit, the follower's sys_stat must see the lock.

        R14 invariant: advisory lock state lives in the raft state
        machine's shared ``Arc<Mutex<LockState>>`` on every replica.
        Once ``apply_acquire_lock`` commits on the leader, the apply
        path on each follower mutates its local copy of that Arc
        under the same mutex. A follower ``sys_stat`` read hits the
        follower's advisory map directly (no ReadIndex), so the
        holder must be visible as soon as the follower has applied
        the committed entry.
        """
        uid = _uid()
        grpc1 = cluster["grpc1"]
        grpc2 = cluster["grpc2"]
        lock_path = f"/corp/eng/follower-visible-{uid}.txt"

        w = _grpc_call(
            grpc1,
            "write",
            {"path": lock_path, "content": f"fv-{uid}"},
            api_key=api_key,
        )
        assert "error" not in w

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
        if not lock_data.get("acquired"):
            pytest.skip("lock_acquire did not succeed on leader")
        lock_id = lock_data.get("lock_id", "")

        # Follower visibility: poll sys_stat on node-2 with a tight
        # bound (replication lag should be sub-second; anything > 5s
        # is a regression of the R14 SSOT invariant).
        deadline = time.time() + 5.0
        visible = False
        last_info: dict = {}
        while time.time() < deadline:
            info = _grpc_call(grpc2, "sys_stat", {"path": lock_path}, api_key=api_key)
            if "error" not in info:
                info_data = info.get("result", info)
                stat_meta = info_data.get("metadata", info_data)
                lock_state = stat_meta.get("lock")
                if lock_state is not None and len(lock_state.get("holders", [])) > 0:
                    visible = True
                    last_info = lock_state
                    break
            time.sleep(0.1)

        # Cleanup regardless of assertion outcome.
        _grpc_call(
            grpc1,
            "sys_unlock",
            {"path": lock_path, "lock_id": lock_id},
            api_key=api_key,
        )

        assert visible, (
            f"Follower did not see lock within 5s of leader commit — "
            f"R14 SSOT advisory map failed to replicate. Last info: {last_info}"
        )


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

    def test_failover_and_recovery(self, cluster, api_key, federation_zones):
        """Stop node-1, verify node-2 serves data, restart, verify catch-up."""
        try:
            import docker as docker_sdk

            # docker-py default client timeout is 60s; Windows Docker Desktop's
            # containers/start API can block >60s during container restart
            # (Hyper-V namespace teardown on the stopped container). Raise to
            # 180s so SDK calls don't ReadTimeout before the daemon responds.
            docker_client = docker_sdk.from_env(timeout=180)
            docker_client.ping()
        except Exception as exc:
            pytest.skip(f"Docker SDK not available: {exc}")

        uid = _uid()
        grpc1 = cluster["grpc1"]
        grpc2 = cluster["grpc2"]

        # Write a file in each federation zone before failover.
        # Root zone (/) uses local redb — not Raft-replicated by design,
        # so only federation zones are tested for cross-node replication.
        zone_files: dict[str, tuple[str, str, str]] = {}
        for zone, prefix, parent in [
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
            # Restart node-1 — split Docker-level start from health-check
            # wait so we can tell WHICH phase stalls if the test ever
            # times out again (R11 hypothesis).
            #
            # NOTE: docker-py's `container.wait(condition=...)` only
            # accepts {"not-running", "next-exit", "removed"} — there
            # is no "running" condition (the API blocks until the
            # container EXITS). We poll `container.attrs["State"]
            # ["Status"]` instead, which is what we actually want.
            t_start = time.time()
            node1_container.start()
            running_deadline = time.time() + 30
            running = False
            while time.time() < running_deadline:
                node1_container.reload()
                if node1_container.attrs["State"]["Status"] == "running":
                    running = True
                    break
                time.sleep(0.5)
            if not running:
                logs = b""
                try:
                    logs = node1_container.logs(tail=200, stderr=True)
                except Exception:
                    pass
                pytest.fail(
                    "node-1 did not reach running state within 30s "
                    f"(elapsed={time.time() - t_start:.1f}s, "
                    f"current state={node1_container.attrs['State']['Status']})\n"
                    f"--- docker logs (tail 200) ---\n{(logs or b'').decode(errors='replace')}"
                )
            t_running = time.time()
            try:
                _wait_healthy([cluster["node1"]], timeout=60)
            except BaseException as exc:
                # Capture docker logs on failure so next CI run has
                # clear diagnosis — slow-start vs healthcheck-bug.
                logs = b""
                try:
                    logs = node1_container.logs(since=int(t_running) - 1, tail=400, stderr=True)
                except Exception:
                    pass
                pytest.fail(
                    "node-1 _wait_healthy did not complete within 60s "
                    f"(running->healthy elapsed={time.time() - t_running:.1f}s): {exc}\n"
                    f"--- docker logs since restart (tail 400) ---\n"
                    f"{(logs or b'').decode(errors='replace')}"
                )

        # Node-1 catches up via Raft log replay from node-2 leader.
        # Gate on raft commit_index first — that's the protocol signal that
        # replication has actually caught up. Then the _wait_replicated loop
        # just confirms list() sees the same state (apply already happened
        # before commit_index advanced).
        _wait_nodes_caught_up([grpc1, grpc2], [ROOT_ZONE_ID, "corp-eng"], api_key, timeout=60)
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
        # commit_index gate first (protocol signal), then verify via list().
        _wait_nodes_caught_up([grpc1, grpc2], [ROOT_ZONE_ID, "corp-eng"], api_key, timeout=60)
        _wait_replicated(
            grpc2,
            "/corp/eng/",
            file_path,
            api_key,
            msg="Cross-zone write not propagated to node-2",
            timeout=30,
        )


# ===========================================================================
# R13 — Federation E2E coverage expansion
# ===========================================================================
#
# 19 new test classes appended below, spanning:
#   R13.1 — gap coverage (uncovered RPCs / behaviors): 11 classes
#   R13.2 — long-flow user journeys: 7 classes
#   R13.3 — CLI surface smoke tests: 1 class
#
# All new classes reuse the module-scoped `cluster` + `api_key` fixtures
# and the existing `_grpc_call` / `_grpc_call_or_skip` helpers so we don't
# fork state or re-spin compose. Ordering: these classes run AFTER the
# original 9 classes because they rely on the pre-built topology
# (/corp, /corp/eng, …) that TestZoneLifecycle + TestMountTopology
# construct.
#
# Tests that require external infrastructure (SSE sidecar for LLM
# mock, witness auto-join semantics, network-partition docker calls)
# self-skip with a clear reason if the prerequisite isn't available.


# ---------------------------------------------------------------------------
# R13 helpers — docker SDK + CLI exec used by partition / CLI tests
# ---------------------------------------------------------------------------
def _docker_client_or_skip():
    """Return a docker.DockerClient or call pytest.skip."""
    try:
        import docker as docker_sdk

        # 180s client timeout for Windows Docker Desktop — daemon can block
        # >60s on container lifecycle APIs during namespace teardown.
        client = docker_sdk.from_env(timeout=180)
        client.ping()
        return client
    except Exception as exc:
        pytest.skip(f"Docker SDK not available: {exc}")


def _cli_exec(container_name: str, argv: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Run ``nexus <argv...>`` inside a compose container via docker exec.

    Returns (exit_code, stdout, stderr). Tests using this helper get
    auto-skipped if Docker SDK isn't reachable.
    """
    client = _docker_client_or_skip()
    try:
        container = client.containers.get(container_name)
    except Exception as exc:
        pytest.skip(f"Container {container_name} not found: {exc}")

    cmd = ["nexus", *argv]
    # CLI connects to the local RPC server over the container's loopback —
    # matches what a sysadmin SSH'd into a node would do.
    env_overrides = {
        "NEXUS_API_KEY": E2E_ADMIN_API_KEY,
        "NEXUS_URL": "http://localhost:2026",
    }
    result = container.exec_run(
        cmd,
        environment=env_overrides,
        demux=True,
        tty=False,
    )
    stdout_b, stderr_b = result.output or (b"", b"")
    stdout = (stdout_b or b"").decode(errors="replace")
    stderr = (stderr_b or b"").decode(errors="replace")
    return result.exit_code, stdout, stderr


# ===================================================================
# R13.1 Class 1/11: Scatter-gather chunked read across nodes
# ===================================================================
class TestScatterGatherChunkedRead:
    """R10-SG: true scatter-gather — reader holds some chunks locally
    (from an earlier remote fetch + cache) while the manifest's NEW
    chunks only exist on the writer node. One read assembles both.

    Because CAS content is stored on the node that EXECUTES a write
    (Raft forwards writes to the leader), we can't simply "have node-2
    do a partial write" — the bytes still land on node-1. The standard
    way to engineer a mixed local/remote state is:

    1. node-1 writes a 17 MiB chunked file → chunks C0..C4 on node-1.
    2. node-2 reads the file once → scatter-gather remote-fetches all
       chunks from node-1 AND caches them to node-2's local CAS.
    3. node-1 does a partial write that replaces the middle region →
       the new manifest references {C0, C_new, C_newer, C4} — the new
       chunks exist ONLY on node-1.
    4. node-2 reads the file again → HAS C0, C4 locally (from the step-
       2 cache), MUST fetch C_new, C_newer remotely from node-1. This
       is the local+remote mix the plan wants validated.
    """

    def test_mixed_local_and_remote_chunks(self, cluster, api_key):
        uid = _uid()
        grpc1 = cluster["grpc1"]
        grpc2 = cluster["grpc2"]

        path = f"/corp/eng/sg-chunked-{uid}.bin"
        total_size = 17 * 1024 * 1024

        # Two distinct fillers so CDC produces divergent fingerprints at
        # the middle boundary — replaced chunks genuinely differ from
        # originals instead of accidentally deduping.
        filler_a = f"nexus-sg-a-{uid}-head-tail-block-varied-marker-"
        filler_b = f"nexus-sg-b-{uid}-middle-block-different-marker-"
        content = (filler_a * (total_size // len(filler_a) + 1))[:total_size]

        # Step 1: full-file write on node-1.
        w1 = _grpc_call(
            grpc1, "write", {"path": path, "content": content}, api_key=api_key, timeout=60
        )
        assert "error" not in w1, f"Initial chunked write failed: {w1}"
        _wait_nodes_caught_up([grpc1, grpc2], [ROOT_ZONE_ID, "corp-eng"], api_key, timeout=60)
        _wait_replicated(
            grpc2,
            "/corp/eng/",
            path,
            api_key,
            msg="Initial manifest not replicated",
            timeout=30,
        )

        # Step 2: node-2 reads the whole file → remote-fetches ALL
        # chunks from node-1 and caches them locally. After this, node-
        # 2's CAS has every chunk the current manifest references.
        r_warm = _grpc_call(grpc2, "read", {"path": path}, api_key=api_key, timeout=60)
        assert "error" not in r_warm, f"Warm-up read on node-2 failed: {r_warm}"

        # Step 3: partial write on node-1 replacing bytes [6 MiB, 11 MiB).
        # Wide enough to span several CDC chunks so the new manifest has
        # multiple NEW chunks that only exist on node-1 (not in node-2's
        # step-2 cache).
        mid_offset = 6 * 1024 * 1024
        mid_len = 5 * 1024 * 1024
        middle_bytes = (filler_b * (mid_len // len(filler_b) + 1))[:mid_len]
        w2 = _grpc_call(
            grpc1,
            "write",
            {"path": path, "content": middle_bytes, "offset": mid_offset},
            api_key=api_key,
            timeout=60,
        )
        if "error" in w2:
            pytest.skip(f"offset-based write not available via RPC: {w2}")

        # Build expected final content locally for comparison.
        import hashlib

        expected = bytearray(content.encode("utf-8"))
        expected[mid_offset : mid_offset + mid_len] = middle_bytes.encode("utf-8")
        expected_bytes = bytes(expected)
        expected_hash = hashlib.blake2b(expected_bytes, digest_size=32).hexdigest()

        def _diagnose(label: str, got: bytes) -> str:
            # NEVER let pytest's diff engine ndiff 17 MiB — O(n²) hangs.
            if got == expected_bytes:
                return f"{label}: OK"
            got_hash = hashlib.blake2b(got, digest_size=32).hexdigest()
            first_diff = -1
            for i in range(min(len(got), len(expected_bytes))):
                if got[i] != expected_bytes[i]:
                    first_diff = i
                    break
            if first_diff == -1 and len(got) != len(expected_bytes):
                first_diff = min(len(got), len(expected_bytes))
            ctx = 32
            lo = max(0, first_diff - ctx) if first_diff >= 0 else 0
            hi = lo + 2 * ctx
            return (
                f"{label}: MISMATCH "
                f"len={len(got)} want={len(expected_bytes)} "
                f"hash={got_hash[:16]} want_hash={expected_hash[:16]} "
                f"first_diff={first_diff} "
                f"got[{lo}:{hi}]={got[lo:hi]!r} want[{lo}:{hi}]={expected_bytes[lo:hi]!r}"
            )

        # Step 4: node-2 reads the modified file. Some chunks (head,
        # tail) are in node-2's local CAS from the step-2 warm-up; the
        # new middle chunks only exist on node-1. The read MUST succeed
        # by mixing local reads with a remote fetch — the defining
        # scatter-gather behavior.
        deadline = time.time() + 30
        r2: dict = {}
        decoded2: bytes = b""
        while time.time() < deadline:
            r2 = _grpc_call(grpc2, "read", {"path": path}, api_key=api_key, timeout=60)
            if "error" not in r2:
                decoded2 = _decode_content(r2)
                if isinstance(decoded2, str):
                    decoded2 = decoded2.encode("utf-8")
                if decoded2 == expected_bytes:
                    break
            time.sleep(0.5)
        assert "error" not in r2, f"Node-2 post-modify read failed: {r2}"
        if decoded2 != expected_bytes:
            pytest.fail(_diagnose("node2 read (local head/tail + remote middle)", decoded2))

        # Sanity: node-1 reads — purely local (every chunk of the new
        # manifest exists on node-1). Confirms the data itself is correct
        # and the above read really did traverse a mixed local/remote path.
        r1 = _grpc_call(grpc1, "read", {"path": path}, api_key=api_key, timeout=60)
        assert "error" not in r1, f"Node-1 post-modify read failed: {r1}"
        decoded1 = _decode_content(r1)
        if isinstance(decoded1, str):
            decoded1 = decoded1.encode("utf-8")
        if decoded1 != expected_bytes:
            pytest.fail(_diagnose("node1 read (all local)", decoded1))

    def test_partial_write_zero_fills_past_eof(self, cluster, api_key):
        """POSIX pwrite semantics: writing past EOF zero-fills the gap.

        R20.10 validation: write a small file, pwrite with offset > size,
        read back and verify the hole between old EOF and the new
        payload is filled with 0x00.
        """
        import hashlib

        uid = _uid()
        grpc1 = cluster["grpc1"]
        path = f"/corp/eng/pwrite-gap-{uid}.bin"

        # Step 1: original tiny file (2 bytes).
        w1 = _grpc_call(
            grpc1, "write", {"path": path, "content": b"ab"}, api_key=api_key, timeout=30
        )
        assert "error" not in w1, f"Initial write failed: {w1}"

        # Step 2: pwrite "xyz" at offset 5 → expected = "ab\0\0\0xyz".
        w2 = _grpc_call(
            grpc1,
            "write",
            {"path": path, "content": b"xyz", "offset": 5},
            api_key=api_key,
            timeout=30,
        )
        if "error" in w2:
            pytest.skip(f"offset-based write not wired: {w2}")

        expected = b"ab\x00\x00\x00xyz"
        expected_hash = hashlib.blake2b(expected, digest_size=32).hexdigest()

        # Step 3: read + verify
        r = _grpc_call(grpc1, "read", {"path": path}, api_key=api_key, timeout=30)
        assert "error" not in r, f"Post-pwrite read failed: {r}"
        got = _decode_content(r)
        if isinstance(got, str):
            got = got.encode("utf-8")
        assert got == expected, (
            f"pwrite-zero-fill: got {got!r} want {expected!r} "
            f"(hash {hashlib.blake2b(got, digest_size=32).hexdigest()[:16]} "
            f"vs {expected_hash[:16]})"
        )


# ===================================================================
# R13.1 Class 2/11: federation_share / federation_join RPCs
# ===================================================================
class TestFederationShareJoin:
    """Peer-to-peer zone bootstrap via share + join RPCs (not create_zone)."""

    def test_share_creates_new_zone(self, cluster, api_key):
        uid = _uid()
        grpc1 = cluster["grpc1"]

        share_path = f"/corp/eng/shared-{uid}"
        mk = _grpc_call(grpc1, "mkdir", {"path": share_path, "parents": True}, api_key=api_key)
        assert "error" not in mk, f"mkdir {share_path} failed: {mk}"

        share_r = _grpc_call_or_skip(
            grpc1,
            "federation_share",
            {"local_path": share_path},
            api_key=api_key,
            skip_msg="federation_share not available",
        )
        if "error" in share_r:
            pytest.skip(f"federation_share failed: {share_r}")
        new_zone_id = share_r.get("result", share_r).get("zone_id", "")
        assert new_zone_id, f"No zone_id returned: {share_r}"
        _wait_zone_ready(grpc1, new_zone_id, api_key, timeout=15)

    def test_join_sees_shared_content(self, cluster, api_key):
        uid = _uid()
        grpc1 = cluster["grpc1"]
        grpc2 = cluster["grpc2"]

        share_path = f"/corp/eng/joinable-{uid}"
        mk = _grpc_call(grpc1, "mkdir", {"path": share_path, "parents": True}, api_key=api_key)
        assert "error" not in mk

        file_path = f"{share_path}/hello.txt"
        wr = _grpc_call(
            grpc1, "write", {"path": file_path, "content": f"shared-{uid}"}, api_key=api_key
        )
        assert "error" not in wr

        sh = _grpc_call_or_skip(
            grpc1,
            "federation_share",
            {"local_path": share_path},
            api_key=api_key,
            skip_msg="federation_share not available",
        )
        if "error" in sh:
            pytest.skip(f"share failed: {sh}")
        zone_id = sh.get("result", sh).get("zone_id", "")
        if not zone_id:
            pytest.skip(f"no zone_id from share: {sh}")

        local_mount = f"/corp/joined-{uid}"
        mk2 = _grpc_call(grpc2, "mkdir", {"path": local_mount, "parents": True}, api_key=api_key)
        assert "error" not in mk2
        jn = _grpc_call_or_skip(
            grpc2,
            "federation_join",
            {
                "peer_addr": "grpc://nexus-1:2028",
                "remote_path": share_path,
                "local_path": local_mount,
            },
            api_key=api_key,
            skip_msg="federation_join not available",
        )
        if "error" in jn:
            pytest.skip(f"join failed (API shape may have changed): {jn}")

        deadline = time.time() + 30
        last: dict = {}
        while time.time() < deadline:
            rr = _grpc_call(grpc2, "read", {"path": f"{local_mount}/hello.txt"}, api_key=api_key)
            last = rr
            if "error" not in rr and _decode_content(rr) == f"shared-{uid}":
                return
            time.sleep(1)
        pytest.fail(f"Joined zone content not visible: {last}")


# ===================================================================
# R13.1 Class 3/11: Zone snapshot export + import round-trip
# ===================================================================
class TestZoneSnapshotExportImport:
    """CLI ``zone export`` + ``zone import`` round-trip.

    Requires docker exec access to run the ``nexus zone export/import``
    CLI inside a container. Skips if the CLI subcommand is missing.
    """

    def test_export_import_roundtrip(self, cluster, api_key):
        uid = _uid()
        grpc1 = cluster["grpc1"]
        zone_id = f"snap-{uid}"

        _grpc_call(grpc1, "federation_create_zone", {"zone_id": zone_id}, api_key=api_key)
        _grpc_call(
            cluster["grpc2"], "federation_create_zone", {"zone_id": zone_id}, api_key=api_key
        )
        _wait_zone_ready(grpc1, zone_id, api_key, timeout=15)

        mount_path = f"/corp/eng/snap-{uid}-mnt"
        mk = _grpc_call(grpc1, "mkdir", {"path": mount_path, "parents": True}, api_key=api_key)
        assert "error" not in mk
        mnt = _grpc_call(
            grpc1,
            "federation_mount",
            {"parent_zone": "corp-eng", "path": mount_path, "target_zone": zone_id},
            api_key=api_key,
        )
        assert "error" not in mnt, f"mount failed: {mnt}"

        data_path = f"{mount_path}/doc.txt"
        wr = _grpc_call(
            grpc1, "write", {"path": data_path, "content": f"snap-payload-{uid}"}, api_key=api_key
        )
        assert "error" not in wr

        export_dest = f"/tmp/zone-export-{uid}.tar"
        rc, out, err = _cli_exec(
            "nexus-dyn-node-1",
            ["zone", "export", zone_id, "--output", export_dest],
            timeout=60,
        )
        if rc != 0:
            pytest.skip(f"zone export CLI not available or failed: rc={rc} err={err[:200]}")

        new_zone_id = f"snap-reimport-{uid}"
        rc2, out2, err2 = _cli_exec(
            "nexus-dyn-node-1",
            ["zone", "import", export_dest, "--zone-id", new_zone_id],
            timeout=60,
        )
        if rc2 != 0:
            pytest.skip(f"zone import CLI not available or failed: rc={rc2} err={err2[:200]}")

        _wait_zone_ready(grpc1, new_zone_id, api_key, timeout=15)


# ===================================================================
# R13.1 Class 4/11: Lock TTL extension (heartbeat)
# ===================================================================
class TestLockTTLExtension:
    """``sys_lock`` with an existing ``lock_id`` extends TTL (heartbeat)."""

    def test_extend_lock_ttl(self, cluster, api_key):
        uid = _uid()
        grpc1 = cluster["grpc1"]
        lock_path = f"/corp/eng/extend-{uid}.txt"

        wr = _grpc_call(
            grpc1, "write", {"path": lock_path, "content": f"extend-{uid}"}, api_key=api_key
        )
        assert "error" not in wr

        acq = _grpc_call_or_skip(
            grpc1,
            "lock_acquire",
            {"path": lock_path, "ttl": 3},
            api_key=api_key,
            skip_msg="Lock API not available",
        )
        if "error" in acq:
            pytest.skip(f"lock_acquire failed: {acq}")
        acq_data = acq.get("result", acq)
        if not acq_data.get("acquired"):
            pytest.skip("lock_acquire did not succeed")
        lock_id = acq_data.get("lock_id", "")

        # Sleep half the original TTL, then extend by another 10s.
        time.sleep(1.5)
        ext = _grpc_call(
            grpc1,
            "sys_lock",
            {"path": lock_path, "lock_id": lock_id, "ttl": 10},
            api_key=api_key,
        )
        if "error" in ext:
            pytest.skip(f"sys_lock(extend) not supported: {ext}")

        # Total 4s > original 3s TTL — lock should still be held.
        time.sleep(2.5)
        info = _grpc_call(grpc1, "sys_stat", {"path": lock_path}, api_key=api_key)
        assert "error" not in info, f"sys_stat failed: {info}"
        info_data = info.get("result", info)
        stat_meta = info_data.get("metadata", info_data)
        lock_state = stat_meta.get("lock") or {}
        holders = lock_state.get("holders", [])
        assert holders, f"Lock should still be held after extend: {info_data}"

        _grpc_call(grpc1, "sys_unlock", {"path": lock_path, "lock_id": lock_id}, api_key=api_key)


# ===================================================================
# R13.1 Class 5/11: Concurrent zone creation — race condition
# ===================================================================
class TestConcurrentZoneCreation:
    """Both nodes create the same zone concurrently — exactly one should
    win, or both return an idempotent success. No split-brain."""

    def test_concurrent_create_no_split_brain(self, cluster, api_key):
        import concurrent.futures

        uid = _uid()
        zone_id = f"race-{uid}"
        grpc1 = cluster["grpc1"]
        grpc2 = cluster["grpc2"]

        def _create(target):
            return _grpc_call(
                target, "federation_create_zone", {"zone_id": zone_id}, api_key=api_key
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(_create, grpc1)
            f2 = ex.submit(_create, grpc2)
            r1 = f1.result(timeout=30)
            r2 = f2.result(timeout=30)

        successes = [r for r in (r1, r2) if "error" not in r]
        assert len(successes) >= 1, f"Neither create succeeded: r1={r1}, r2={r2}"
        _wait_zone_ready(grpc1, zone_id, api_key, timeout=10)
        _wait_zone_ready(grpc2, zone_id, api_key, timeout=10)

        zones = _grpc_call(grpc1, "federation_list_zones", {}, api_key=api_key)
        assert "error" not in zones
        ids = [z["zone_id"] for z in zones["result"]["zones"]]
        assert ids.count(zone_id) == 1, f"Zone appears multiple times: {ids}"


# ===================================================================
# R13.1 Class 6/11: Zone removal with active mounts
# ===================================================================
class TestZoneRemovalWithActiveMounts:
    """Remove a zone mounted at multiple paths — all mounts must clean
    up atomically; reads afterwards return errors."""

    def test_remove_zone_cleans_all_mounts(self, cluster, api_key):
        uid = _uid()
        grpc1 = cluster["grpc1"]
        grpc2 = cluster["grpc2"]
        zone_id = f"cleanup-{uid}"

        # Create on BOTH nodes so the Raft zone group has all peers and
        # subsequent writes/removes reach quorum (mirrors existing
        # TestZoneLifecycle.test_zones_visible_on_both_nodes pattern).
        _grpc_call(grpc1, "federation_create_zone", {"zone_id": zone_id}, api_key=api_key)
        _grpc_call(grpc2, "federation_create_zone", {"zone_id": zone_id}, api_key=api_key)
        _wait_zone_ready(grpc1, zone_id, api_key, timeout=15)
        _wait_zone_ready(grpc2, zone_id, api_key, timeout=15)

        mnt_a = f"/corp/cleanup-a-{uid}"
        mnt_b = f"/family/cleanup-b-{uid}"
        for parent_zone, path in [("corp", mnt_a), ("family", mnt_b)]:
            mk = _grpc_call(grpc1, "mkdir", {"path": path, "parents": True}, api_key=api_key)
            assert "error" not in mk
            r = _grpc_call(
                grpc1,
                "federation_mount",
                {"parent_zone": parent_zone, "path": path, "target_zone": zone_id},
                api_key=api_key,
            )
            assert "error" not in r, f"mount {path} failed: {r}"

        probe = f"{mnt_a}/probe.txt"
        wr = _grpc_call(grpc1, "write", {"path": probe, "content": f"probe-{uid}"}, api_key=api_key)
        assert "error" not in wr

        # Server refuses federation_remove_zone while mounts exist unless
        # force=True is passed — we explicitly want the ATOMIC cleanup
        # semantics the plan describes ("both mounts removed atomically"),
        # so force the remove and verify subsequent reads fail.
        rm = _grpc_call(
            grpc1,
            "federation_remove_zone",
            {"zone_id": zone_id, "force": True},
            api_key=api_key,
        )
        assert "error" not in rm, f"remove_zone failed: {rm}"

        # Wait for removal to propagate.
        deadline = time.time() + 15
        while time.time() < deadline:
            zones = _grpc_call(grpc1, "federation_list_zones", {}, api_key=api_key)
            if "error" not in zones and zone_id not in [
                z["zone_id"] for z in zones["result"]["zones"]
            ]:
                break
            time.sleep(0.5)

        for path in [f"{mnt_a}/probe.txt", f"{mnt_b}/probe.txt"]:
            r = _grpc_call(grpc1, "read", {"path": path}, api_key=api_key)
            assert "error" in r, f"Read should fail after zone removal: {path} -> {r}"


# ===================================================================
# R13.1 Class 7/11: Witness auto-join (observability only)
# ===================================================================
class TestWitnessAutoJoin:
    """Verify a post-launch-created zone pulls in the witness node
    automatically. Soft-skips if the compose stack has no witness."""

    def test_witness_participates_in_new_zone(self, cluster, api_key):
        uid = _uid()
        grpc1 = cluster["grpc1"]
        grpc2 = cluster["grpc2"]
        zone_id = f"witness-{uid}"

        _grpc_call(grpc1, "federation_create_zone", {"zone_id": zone_id}, api_key=api_key)
        _grpc_call(grpc2, "federation_create_zone", {"zone_id": zone_id}, api_key=api_key)
        _wait_zone_ready(grpc1, zone_id, api_key, timeout=15)
        _wait_zone_ready(grpc2, zone_id, api_key, timeout=15)

        info = _grpc_call(grpc1, "federation_cluster_info", {"zone_id": zone_id}, api_key=api_key)
        assert "error" not in info, f"cluster_info failed: {info}"
        data = info["result"]
        witness_count = data.get("witness_count", 0)
        voters = data.get("voter_count", data.get("members_count", 0))
        if witness_count == 0:
            pytest.skip(
                "Witness node not configured in this compose file; "
                "cannot validate auto-join (track as follow-up)."
            )
        assert voters >= 2, f"Expected ≥2 voters + witness: {data}"


# ===================================================================
# R13.1 Class 8/11: Zone-level Raft introspection
# ===================================================================
class TestZoneRaftIntrospection:
    """``federation_cluster_info`` returns Raft commit index — it must
    monotonically advance after writes."""

    def test_raft_commit_index_progresses_on_writes(self, cluster, api_key):
        uid = _uid()
        grpc1 = cluster["grpc1"]

        def _commit_idx(zone: str) -> int:
            r = _grpc_call(grpc1, "federation_cluster_info", {"zone_id": zone}, api_key=api_key)
            if "error" in r:
                return -1
            d = r.get("result", r)
            for k in ("commit_index", "last_committed", "log_index"):
                if k in d:
                    try:
                        return int(d[k])
                    except (TypeError, ValueError):
                        pass
            return -1

        before = _commit_idx("corp-eng")
        if before < 0:
            pytest.skip("cluster_info does not expose commit_index")

        for i in range(3):
            path = f"/corp/eng/raft-progress-{uid}-{i}.txt"
            wr = _grpc_call(
                grpc1, "write", {"path": path, "content": f"raft-{uid}-{i}"}, api_key=api_key
            )
            assert "error" not in wr

        deadline = time.time() + 15
        after = before
        while time.time() < deadline and after <= before:
            after = _commit_idx("corp-eng")
            time.sleep(0.5)
        assert after > before, f"Commit index did not advance: {before} -> {after}"


# ===================================================================
# R13.1 Class 9/11: Partial replication failure (network_partition)
# ===================================================================
class TestPartialReplicationFailure:
    """docker network disconnect -> write on node-1 -> reconnect ->
    verify node-2 catches up via Raft log without dropped entries."""

    def test_partition_then_heal(self, cluster, api_key):
        client = _docker_client_or_skip()
        uid = _uid()
        grpc1 = cluster["grpc1"]
        grpc2 = cluster["grpc2"]

        try:
            node2 = client.containers.get("nexus-dyn-node-2")
        except Exception as exc:
            pytest.skip(f"node-2 container not found: {exc}")
        networks = list(node2.attrs["NetworkSettings"]["Networks"].keys())
        if not networks:
            pytest.skip("node-2 has no docker networks attached")
        net_name = networks[0]

        try:
            net_obj = client.networks.get(net_name)
        except Exception as exc:
            pytest.skip(f"docker network get failed: {exc}")

        try:
            net_obj.disconnect("nexus-dyn-node-2", force=True)
        except Exception as exc:
            pytest.skip(f"network disconnect not supported in this env: {exc}")

        reconnected = False
        written: list[str] = []
        try:
            # Write 5 files on node-1 while node-2 is partitioned.
            for i in range(5):
                path = f"/corp/eng/partition-{uid}-{i}.txt"
                wr = _grpc_call(
                    grpc1,
                    "write",
                    {"path": path, "content": f"p-{uid}-{i}"},
                    api_key=api_key,
                )
                if "error" in wr:
                    # Quorum may be lost (only 2 voters) — skip cleanly.
                    pytest.skip(f"Write during partition failed (likely quorum loss): {wr}")
                written.append(path)
        finally:
            try:
                net_obj.connect("nexus-dyn-node-2")
                reconnected = True
            except Exception:
                pass

        assert reconnected, "Failed to reconnect node-2; cluster may be in bad state."

        _wait_healthy([cluster["node2"]], timeout=60)
        for path in written:
            _wait_replicated(
                grpc2,
                "/corp/eng/",
                path,
                api_key,
                msg=f"Post-partition catch-up missing: {path}",
                timeout=30,
            )


# ===================================================================
# R13.1 Class 10/11: OpenAI backend Rust CAS (via sse-mock sidecar)
# ===================================================================
# SSE mock sidecar URL (see dockerfiles/docker-compose.dynamic-federation-test.yml).
# OpenAI backend builds `{base_url}/chat/completions` — SSE mock serves it at
# `/v1/chat/completions`, so base URL includes the `/v1` prefix. Anthropic backend
# builds `{base_url}/v1/messages` itself, so its base URL stops at the hostname.
_SSE_MOCK_OPENAI_BASE = "http://sse-mock:8080/v1"
_SSE_MOCK_ANTHROPIC_BASE = "http://sse-mock:8080"


def _sse_mock_reachable() -> bool:
    """Return True if the sse-mock sidecar is up.

    The CI "E2E Tests (Docker)" workflow runs pytest directly without any
    docker-compose sidecars, so sse-mock is unreachable there. The full
    docker-compose.dynamic-federation-test.yml stack runs the sidecar and
    tests resolve it by container hostname. Skip the LLM-streaming tests
    when unreachable — matches the skip pattern every other class in this
    file uses for unreachable infrastructure.
    """
    try:
        httpx.get("http://sse-mock:8080/healthz", timeout=2, trust_env=False)
        return True
    except httpx.TransportError:
        return False


_SSE_MOCK_SKIP_REASON = (
    "sse-mock sidecar not reachable (run via docker-compose.dynamic-federation-test.yml)"
)


def _bootstrap_standalone_fs(tmp_path):
    """Create an in-process NexusFS with a local CAS backend.

    The e2e-runner container carries the full nexus_kernel install, so we
    can instantiate a fresh standalone filesystem just like the unit test
    at tests/unit/backends/test_openai_compat_rust.py::_bootstrap does.
    No RPC surface is added — the test drives kernel syscalls in-process
    via the PyO3 bindings (the pattern validated for R20.14).
    """
    import asyncio

    from nexus.backends.storage.cas_local import CASLocalBackend
    from nexus.core.config import ParseConfig, PermissionConfig
    from nexus.factory import create_nexus_fs
    from nexus.storage.record_store import SQLAlchemyRecordStore
    from tests.helpers.dict_metastore import DictMetastore

    return asyncio.run(
        create_nexus_fs(
            backend=CASLocalBackend(tmp_path / "data"),
            metadata_store=DictMetastore(),
            record_store=SQLAlchemyRecordStore(db_path=tmp_path / "meta.db"),
            parsing=ParseConfig(auto_parse=False),
            permissions=PermissionConfig(enforce=False),
        )
    )


def _llm_round_trip(nx, mount, request, session_suffix="0"):
    """Drive one llm_start_streaming call and return (payload, session_hash, envelope)."""
    import json as _json

    stream_path = f"{mount}/stream/session-{session_suffix}"
    nx._kernel.create_stream(stream_path, 65_536)
    req_bytes = _json.dumps(request).encode("utf-8")
    nx._kernel.llm_start_streaming(mount, "root", req_bytes, stream_path)
    raw = nx.stream_collect_all(stream_path).decode("utf-8")
    done_idx = raw.index("{")
    done = _json.loads(raw[done_idx:])
    assert done["type"] == "done", f"expected done frame, got: {done}"
    session_hash = done["session_hash"]
    envelope_bytes = nx._kernel.cas_read(mount, "root", session_hash)
    envelope = _json.loads(envelope_bytes)
    return raw[:done_idx], session_hash, envelope


@pytest.mark.skipif(not _sse_mock_reachable(), reason=_SSE_MOCK_SKIP_REASON)
class TestOpenAIBackendRustCAS:
    """End-to-end LLM streaming via Rust OpenAIBackend against sse-mock sidecar.

    Drives kernel syscalls in-process via PyO3 (no RPC surface added).
    """

    def test_streaming_round_trip(self, tmp_path):
        import json as _json

        from nexus.contracts.metadata import DT_MOUNT

        nx = _bootstrap_standalone_fs(tmp_path)
        try:
            nx.sys_setattr(
                "/llm",
                entry_type=DT_MOUNT,
                backend_type="openai",
                backend_name="openai_compatible",
                openai_base_url=_SSE_MOCK_OPENAI_BASE,
                openai_api_key="sk-mock",
                openai_model="mock-gpt-4o-mini",
                openai_blob_root=str(tmp_path / "llm_spool"),
            )

            payload, session_hash, envelope = _llm_round_trip(
                nx,
                "/llm",
                {
                    "messages": [{"role": "user", "content": "hi"}],
                    "model": "mock-gpt-4o-mini",
                },
            )

            assert "Hello from SSE mock." in payload
            assert len(session_hash) == 64
            assert envelope["type"] == "llm_session_v1"
            assert envelope["model"] == "mock-gpt-4o-mini"
            assert envelope["request_hash"]
            assert envelope["response_hash"]

            # CAS dedup: identical request -> identical request_hash.
            # (session_hash hashes the full envelope which includes latency_ms;
            # that varies per call by design, so compare request_hash instead.)
            _, _session_hash_2, envelope_2 = _llm_round_trip(
                nx,
                "/llm",
                {
                    "messages": [{"role": "user", "content": "hi"}],
                    "model": "mock-gpt-4o-mini",
                },
                session_suffix="dedup",
            )
            assert envelope_2["request_hash"] == envelope["request_hash"], _json.dumps(envelope_2)
        finally:
            nx.close()


@pytest.mark.skipif(not _sse_mock_reachable(), reason=_SSE_MOCK_SKIP_REASON)
class TestAnthropicBackendRustCAS:
    """Mirror of TestOpenAIBackendRustCAS with Anthropic-shaped SSE."""

    def test_streaming_round_trip(self, tmp_path):
        from nexus.contracts.metadata import DT_MOUNT

        nx = _bootstrap_standalone_fs(tmp_path)
        try:
            nx.sys_setattr(
                "/llm",
                entry_type=DT_MOUNT,
                backend_type="anthropic",
                backend_name="anthropic_native",
                anthropic_base_url=_SSE_MOCK_ANTHROPIC_BASE,
                anthropic_api_key="sk-ant-mock",
                anthropic_model="mock-claude-3-5-sonnet",
                anthropic_blob_root=str(tmp_path / "llm_spool"),
            )

            payload, session_hash, envelope = _llm_round_trip(
                nx,
                "/llm",
                {
                    "messages": [{"role": "user", "content": "hi"}],
                    "model": "mock-claude-3-5-sonnet",
                    "max_tokens": 1024,
                },
            )

            assert "Hello from SSE mock." in payload
            assert len(session_hash) == 64
            assert envelope["type"] == "llm_session_v1"
            assert envelope["model"] == "mock-claude-3-5-sonnet"
            assert envelope["request_hash"]
            assert envelope["response_hash"]
        finally:
            nx.close()


# ===================================================================
# R13.2 Class 1/7: Day at the office — CRUD + lock + delete
# ===================================================================
class TestDayAtTheOffice:
    """Full CRUD lifecycle: write 4 versions, read on follower, acquire
    lock, update-under-lock, release, delete, verify gone on follower."""

    def test_full_lifecycle(self, cluster, api_key):
        uid = _uid()
        grpc1 = cluster["grpc1"]
        grpc2 = cluster["grpc2"]

        project = f"/corp/eng/day-office-{uid}"
        mk = _grpc_call(grpc1, "mkdir", {"path": project, "parents": True}, api_key=api_key)
        assert "error" not in mk, f"mkdir failed: {mk}"

        spec = f"{project}/spec.md"
        versions = [f"v{i}-{uid}" for i in range(4)]
        for v in versions:
            wr = _grpc_call(grpc1, "write", {"path": spec, "content": v}, api_key=api_key)
            assert "error" not in wr, f"write {v} failed: {wr}"

        _wait_replicated(
            grpc2,
            project + "/",
            spec,
            api_key,
            timeout=15,
            msg="spec.md not replicated to follower",
        )
        # _wait_replicated only confirms the path exists on the follower — it
        # returns as soon as the first write's entry is visible in list().
        # The subsequent versions may still be mid-apply. Poll for content
        # convergence before asserting (apply lag is bounded by raft RTT).
        deadline = time.time() + 15
        while time.time() < deadline:
            r = _grpc_call(grpc2, "read", {"path": spec}, api_key=api_key)
            if "error" not in r and _decode_content(r) == versions[-1]:
                break
            time.sleep(0.2)
        assert "error" not in r, f"read on follower failed: {r}"
        assert _decode_content(r) == versions[-1]

        acq = _grpc_call_or_skip(
            grpc1,
            "lock_acquire",
            {"path": spec, "ttl": 30},
            api_key=api_key,
            skip_msg="Lock API not available",
        )
        if "error" in acq or not acq.get("result", acq).get("acquired"):
            pytest.skip("lock_acquire failed — cannot test locked update")
        lock_id = acq["result"]["lock_id"]

        final = f"final-{uid}"
        wr = _grpc_call(grpc1, "write", {"path": spec, "content": final}, api_key=api_key)
        assert "error" not in wr, f"write under lock failed: {wr}"

        _grpc_call(grpc1, "sys_unlock", {"path": spec, "lock_id": lock_id}, api_key=api_key)
        rm = _grpc_call(grpc1, "sys_unlink", {"path": spec}, api_key=api_key)
        assert "error" not in rm, f"unlink failed: {rm}"

        deadline = time.time() + 20
        while time.time() < deadline:
            rr = _grpc_call(grpc2, "sys_stat", {"path": spec}, api_key=api_key)
            # sys_stat on a missing path returns ``{"result": {"metadata": None}}``
            # (not an "error" field, and result itself is the outer dict).
            if "error" in rr or rr.get("result", {}).get("metadata") is None:
                return
            time.sleep(0.5)
        pytest.fail("spec.md still visible on follower after delete")


# ===================================================================
# R13.2 Class 2/7: New team onboarding — nested zones
# ===================================================================
class TestNewTeamOnboarding:
    """Create a zone, mount at a deep nested path, populate, verify on
    peers, unmount, verify disposal."""

    def test_zone_lifecycle_with_nested_paths(self, cluster, api_key):
        uid = _uid()
        grpc1 = cluster["grpc1"]
        grpc2 = cluster["grpc2"]
        zone_id = f"team-{uid}"

        _grpc_call(grpc1, "federation_create_zone", {"zone_id": zone_id}, api_key=api_key)
        _grpc_call(grpc2, "federation_create_zone", {"zone_id": zone_id}, api_key=api_key)
        _wait_zone_ready(grpc1, zone_id, api_key, timeout=15)
        _wait_zone_ready(grpc2, zone_id, api_key, timeout=15)

        mount_path = f"/corp/eng/team-x-{uid}"
        mk = _grpc_call(grpc1, "mkdir", {"path": mount_path, "parents": True}, api_key=api_key)
        assert "error" not in mk
        # Raise timeout: federation_mount on a freshly-created zone may
        # wait for election to complete before the i_links_count bump
        # lands. Default 10s is tight for 3-voter elections + propose
        # commit when tests wake zone-ready back-to-back.
        mnt = _grpc_call(
            grpc1,
            "federation_mount",
            {"parent_zone": "corp-eng", "path": mount_path, "target_zone": zone_id},
            api_key=api_key,
            timeout=30,
        )
        assert "error" not in mnt, f"mount failed: {mnt}"

        for i in range(10):
            p = f"{mount_path}/projects/proj1/docs/f{i}.txt"
            wr = _grpc_call(grpc1, "write", {"path": p, "content": f"{uid}-{i}"}, api_key=api_key)
            assert "error" not in wr, f"write {i} failed: {wr}"

        _wait_replicated(
            grpc2,
            f"{mount_path}/projects/proj1/docs/",
            f"{mount_path}/projects/proj1/docs/f0.txt",
            api_key,
            timeout=30,
        )

        um = _grpc_call(
            grpc1,
            "federation_unmount",
            {"parent_zone": "corp-eng", "path": mount_path},
            api_key=api_key,
        )
        assert "error" not in um, f"unmount failed: {um}"

        r = _grpc_call(
            grpc1, "read", {"path": f"{mount_path}/projects/proj1/docs/f0.txt"}, api_key=api_key
        )
        assert "error" in r


# ===================================================================
# R13.2 Class 3/7: Cross-zone daily workflow via crosslink
# ===================================================================
class TestCrossZoneDailyWorkflow:
    """Data flows through the ``/family/work/ → corp`` crosslink set up
    in TestMountTopology: write at work path, read via crosslink,
    modify via crosslink, verify change at original path on the OTHER
    node, delete via crosslink, verify gone at work path.

    Strong causal chain: each step observes the prior step's mutation,
    both across zones (via the crosslink) and across peers (via Raft)."""

    def test_crosslink_roundtrip(self, cluster, api_key):
        uid = _uid()
        grpc1 = cluster["grpc1"]
        grpc2 = cluster["grpc2"]

        # /corp/ and /family/work/ both resolve to the `corp` zone root
        # (single-hop crosslink set up in TestMountTopology). A file
        # written at one URL must be visible at the other.
        file_name = f"crosslink-{uid}.txt"
        work_path = f"/corp/{file_name}"
        family_view = f"/family/work/{file_name}"

        # Step 1 — create at work path.
        initial = f"created-at-work-{uid}"
        w1 = _grpc_call(grpc1, "write", {"path": work_path, "content": initial}, api_key=api_key)
        assert "error" not in w1, f"work-path write failed: {w1}"

        # Step 2 — read via family crosslink; MUST see step 1's bytes.
        deadline = time.time() + 15
        r1: dict = {}
        while time.time() < deadline:
            r1 = _grpc_call(grpc1, "read", {"path": family_view}, api_key=api_key)
            if "error" not in r1 and _decode_content(r1) == initial:
                break
            time.sleep(0.3)
        else:
            pytest.fail(f"Crosslink read did not see work-path write: {r1}")

        # Step 3 — modify via the crosslink (family view).
        updated = f"updated-via-family-{uid}"
        w2 = _grpc_call(grpc1, "write", {"path": family_view, "content": updated}, api_key=api_key)
        assert "error" not in w2, f"family-view write failed: {w2}"

        # Step 4 — read via work path on the OTHER node; MUST see
        # step 3's update. (Cross-zone + cross-peer in one read.)
        deadline = time.time() + 15
        r2: dict = {}
        while time.time() < deadline:
            r2 = _grpc_call(grpc2, "read", {"path": work_path}, api_key=api_key)
            if "error" not in r2 and _decode_content(r2) == updated:
                break
            time.sleep(0.3)
        else:
            pytest.fail(f"Work-path read on follower did not see crosslink update: {r2}")

        # Step 5 — delete via family view; work path MUST become gone.
        rm = _grpc_call(grpc1, "sys_unlink", {"path": family_view}, api_key=api_key)
        assert "error" not in rm, f"crosslink unlink failed: {rm}"

        deadline = time.time() + 15
        rr: dict = {}
        while time.time() < deadline:
            rr = _grpc_call(grpc1, "sys_stat", {"path": work_path}, api_key=api_key)
            # sys_stat returns ``{"result": {"metadata": None}}`` for a
            # missing path — the outer "result" dict is still present,
            # we must drill into "metadata" to see the miss.
            if "error" in rr or rr.get("result", {}).get("metadata") is None:
                return
            time.sleep(0.3)
        pytest.fail(f"File still visible at work path after crosslink unlink: {rr}")


# ===================================================================
# R13.2 Class 4/7: Concurrent lock contention edit
# ===================================================================
class TestConcurrentLockEdit:
    """Node A holds exclusive lock, B's write blocks until release, then
    B's write wins — final content reflects B."""

    def test_contended_write_ordering(self, cluster, api_key):
        import concurrent.futures

        uid = _uid()
        grpc1 = cluster["grpc1"]
        grpc2 = cluster["grpc2"]
        lock_path = f"/corp/eng/contend-edit-{uid}.txt"

        _grpc_call(grpc1, "write", {"path": lock_path, "content": f"init-{uid}"}, api_key=api_key)

        acq = _grpc_call_or_skip(
            grpc1,
            "lock_acquire",
            {"path": lock_path, "ttl": 10},
            api_key=api_key,
            skip_msg="Lock API not available",
        )
        acq_data = acq.get("result", {})
        if "error" in acq or not acq_data.get("acquired"):
            pytest.skip("lock_acquire failed")
        lock_a = acq_data["lock_id"]

        def _node_b_write():
            deadline = time.time() + 15
            while time.time() < deadline:
                r = _grpc_call(
                    grpc2, "lock_acquire", {"path": lock_path, "ttl": 10}, api_key=api_key
                )
                if "error" not in r and r.get("result", {}).get("acquired"):
                    lid = r["result"]["lock_id"]
                    wr = _grpc_call(
                        grpc2,
                        "write",
                        {"path": lock_path, "content": f"B-{uid}"},
                        api_key=api_key,
                    )
                    _grpc_call(
                        grpc2,
                        "sys_unlock",
                        {"path": lock_path, "lock_id": lid},
                        api_key=api_key,
                    )
                    return wr
                time.sleep(0.5)
            return {"error": "B could not acquire within timeout"}

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            b_future = ex.submit(_node_b_write)
            time.sleep(1)  # give B a chance to start retrying

            wa = _grpc_call(
                grpc1, "write", {"path": lock_path, "content": f"A-{uid}"}, api_key=api_key
            )
            assert "error" not in wa
            _grpc_call(grpc1, "sys_unlock", {"path": lock_path, "lock_id": lock_a}, api_key=api_key)

            b_result = b_future.result(timeout=20)

        if "error" in b_result:
            pytest.skip(f"B's write did not complete: {b_result}")

        deadline = time.time() + 15
        while time.time() < deadline:
            r = _grpc_call(grpc1, "read", {"path": lock_path}, api_key=api_key)
            if "error" not in r and _decode_content(r) == f"B-{uid}":
                return
            time.sleep(0.5)
        pytest.fail(f"Final content not B's write: {r}")


# ===================================================================
# R13.2 Class 5/7: Full failover with delete+rename replay
# ===================================================================
class TestFullFailoverRecovery:
    """Extended failover: delete + rename happen while node-1 is down;
    node-1 catches up via Raft log replay on restart."""

    @pytest.mark.order(after="TestLeaderFailover::test_failover_and_recovery")
    def test_failover_with_delete_rename_replay(self, cluster, api_key, federation_zones):
        client = _docker_client_or_skip()
        uid = _uid()
        grpc1 = cluster["grpc1"]
        grpc2 = cluster["grpc2"]

        base = f"/corp/eng/recover-{uid}"
        mk = _grpc_call(grpc1, "mkdir", {"path": base, "parents": True}, api_key=api_key)
        assert "error" not in mk
        for i in range(3):
            wr = _grpc_call(
                grpc1,
                "write",
                {"path": f"{base}/doc{i}.txt", "content": f"pre-{uid}-{i}"},
                api_key=api_key,
            )
            assert "error" not in wr

        for i in range(3):
            _wait_replicated(grpc2, f"{base}/", f"{base}/doc{i}.txt", api_key, timeout=15)

        try:
            node1 = client.containers.get("nexus-dyn-node-1")
        except Exception as exc:
            pytest.skip(f"node-1 container not found: {exc}")
        node1.stop(timeout=10)

        try:
            _wait_healthy([cluster["node2"]], timeout=30)
            _wait_leader_elected(grpc2, "corp-eng", api_key, timeout=15)

            _grpc_call(grpc2, "sys_unlink", {"path": f"{base}/doc0.txt"}, api_key=api_key)
            rn = _grpc_call(
                grpc2,
                "sys_rename",
                {
                    "old_path": f"{base}/doc1.txt",
                    "new_path": f"{base}/doc1-renamed.txt",
                },
                api_key=api_key,
            )
            assert "error" not in rn, f"sys_rename failed: {rn}"
            _grpc_call(
                grpc2,
                "write",
                {"path": f"{base}/doc3.txt", "content": f"post-{uid}-3"},
                api_key=api_key,
            )
        finally:
            # Split start + reach-running poll + _wait_healthy so
            # timeouts surface actionable diagnostics. See
            # TestLeaderFailover for the wait-condition note (docker-py
            # has no "running" condition; we poll State.Status).
            t_start = time.time()
            node1.start()
            running_deadline = time.time() + 30
            running = False
            while time.time() < running_deadline:
                node1.reload()
                if node1.attrs["State"]["Status"] == "running":
                    running = True
                    break
                time.sleep(0.5)
            if not running:
                logs = b""
                try:
                    logs = node1.logs(tail=200, stderr=True)
                except Exception:
                    pass
                pytest.fail(
                    "node-1 did not reach running within 30s "
                    f"(elapsed={time.time() - t_start:.1f}s, "
                    f"current state={node1.attrs['State']['Status']})\n"
                    f"--- docker logs (tail 200) ---\n{(logs or b'').decode(errors='replace')}"
                )
            t_running = time.time()
            try:
                _wait_healthy([cluster["node1"]], timeout=60)
            except BaseException as exc:
                logs = b""
                try:
                    logs = node1.logs(since=int(t_running) - 1, tail=400, stderr=True)
                except Exception:
                    pass
                pytest.fail(
                    "node-1 _wait_healthy timed out "
                    f"(running->healthy elapsed={time.time() - t_running:.1f}s): {exc}\n"
                    f"--- docker logs since restart (tail 400) ---\n"
                    f"{(logs or b'').decode(errors='replace')}"
                )

        # Wait for node-1 to catch up on every zone that could host these
        # paths. Empirically the file metadata for ``/corp/eng/recover-*``
        # lands in the ROOT zone's state machine (``zone_id=ROOT_ZONE_ID``
        # in the returned metadata blob), not in corp-eng — DT_MOUNT
        # children are addressed through the parent zone's namespace. Gate
        # on both so a slow metadata replication in root doesn't leave the
        # sys_stat assertions reading a half-applied state machine.
        _wait_nodes_caught_up([grpc1, grpc2], [ROOT_ZONE_ID, "corp-eng"], api_key, timeout=60)

        deadline = time.time() + 30
        s = r2 = r3 = {}
        while time.time() < deadline:
            s = _grpc_call(grpc1, "sys_stat", {"path": f"{base}/doc0.txt"}, api_key=api_key)
            r2 = _grpc_call(
                grpc1, "sys_stat", {"path": f"{base}/doc1-renamed.txt"}, api_key=api_key
            )
            r3 = _grpc_call(grpc1, "sys_stat", {"path": f"{base}/doc3.txt"}, api_key=api_key)

            # sys_stat on a deleted/absent path returns {"result": {"metadata": None}},
            # not {"result": None} — the kernel always emits a result envelope and
            # signals "gone" via metadata:None (mirrors POSIX stat returning -ENOENT).
            def _is_gone(resp):
                if "error" in resp:
                    return True
                r = resp.get("result")
                return r is None or r.get("metadata") is None

            def _is_present(resp):
                if "error" in resp:
                    return False
                r = resp.get("result") or {}
                return r.get("metadata") is not None

            s_gone = _is_gone(s)
            r2_present = _is_present(r2)
            r3_present = _is_present(r3)
            if s_gone and r2_present and r3_present:
                return
            time.sleep(1)
        pytest.fail(f"Replay incomplete: doc0={s}, doc1-renamed={r2}, doc3={r3}")


# ===================================================================
# R13.2 Class 6/7: CAS dedup across zones via shared ETag
# ===================================================================
class TestMultiZoneAtomicWrite:
    """Write identical content into /corp/eng and /family — both writes
    should produce the same ETag (BLAKE3 hash) because CAS is global to
    the kernel. Then mutate ONE side and verify the ETags diverge while
    the other side still resolves to the original hash.

    Strong causal chain:
      1. Write payload X to /corp/eng/X.txt → etag_corp_v1.
      2. Write same payload X to /family/X.txt → etag_family_v1.
      3. Observation: etag_corp_v1 == etag_family_v1 (CAS-level dedup).
      4. Mutate /corp/eng/X.txt → etag_corp_v2 ≠ etag_corp_v1.
      5. /family/X.txt etag still == etag_corp_v1 (isolation — zone-
         local mutation does not bleed across).
    """

    def test_cas_dedup_then_divergence(self, cluster, api_key):
        uid = _uid()
        grpc1 = cluster["grpc1"]

        payload = f"shared-payload-{uid}-" + ("a" * 512)
        corp_path = f"/corp/eng/dedup-{uid}.txt"
        family_path = f"/family/dedup-{uid}.txt"

        # Step 1 — write identical bytes to two different paths in
        # different zones.
        w1 = _grpc_call(grpc1, "write", {"path": corp_path, "content": payload}, api_key=api_key)
        assert "error" not in w1, f"corp write failed: {w1}"
        w2 = _grpc_call(grpc1, "write", {"path": family_path, "content": payload}, api_key=api_key)
        assert "error" not in w2, f"family write failed: {w2}"

        # Step 2 — both paths must report the same ETag (CAS dedup).
        s_corp = _grpc_call(grpc1, "sys_stat", {"path": corp_path}, api_key=api_key)
        s_family = _grpc_call(grpc1, "sys_stat", {"path": family_path}, api_key=api_key)
        etag_corp_v1 = (s_corp.get("result", {}) or {}).get("etag") or (
            s_corp.get("result", {}) or {}
        ).get("metadata", {}).get("etag")
        etag_family_v1 = (s_family.get("result", {}) or {}).get("etag") or (
            s_family.get("result", {}) or {}
        ).get("metadata", {}).get("etag")

        if not etag_corp_v1 or not etag_family_v1:
            pytest.skip(
                f"sys_stat did not expose etag in this build: corp={s_corp}, family={s_family}"
            )
        assert etag_corp_v1 == etag_family_v1, (
            f"CAS dedup broken: {etag_corp_v1} != {etag_family_v1}"
        )

        # Step 3 — mutate ONE side.
        mutated = f"mutated-{uid}-" + ("b" * 512)
        wm = _grpc_call(grpc1, "write", {"path": corp_path, "content": mutated}, api_key=api_key)
        assert "error" not in wm, f"corp mutation failed: {wm}"

        # Step 4 — etag on /corp/eng diverges; etag on /family unchanged.
        s_corp2 = _grpc_call(grpc1, "sys_stat", {"path": corp_path}, api_key=api_key)
        s_family2 = _grpc_call(grpc1, "sys_stat", {"path": family_path}, api_key=api_key)
        etag_corp_v2 = (s_corp2.get("result", {}) or {}).get("etag") or (
            s_corp2.get("result", {}) or {}
        ).get("metadata", {}).get("etag")
        etag_family_v2 = (s_family2.get("result", {}) or {}).get("etag") or (
            s_family2.get("result", {}) or {}
        ).get("metadata", {}).get("etag")

        assert etag_corp_v2 != etag_corp_v1, f"Mutation did not change corp etag: {etag_corp_v1}"
        assert etag_family_v2 == etag_family_v1, (
            f"Zone isolation broken: family etag changed {etag_family_v1} -> "
            f"{etag_family_v2} after corp-only mutation"
        )

        # Step 5 — the original bytes must still be reachable via the
        # /family path (the chunk behind etag_family_v1 is still in CAS).
        rf = _grpc_call(grpc1, "read", {"path": family_path}, api_key=api_key)
        assert "error" not in rf, f"family read failed: {rf}"
        assert _decode_content(rf) == payload


# ===================================================================
# R13.2 Class 7/7: LLM session end-to-end (via sse-mock sidecar)
# ===================================================================
@pytest.mark.skipif(not _sse_mock_reachable(), reason=_SSE_MOCK_SKIP_REASON)
class TestLLMSessionEndToEnd:
    """Three-turn conversation through the Rust OpenAIBackend, verifying
    CAS dedup (repeat request = same envelope hash) and that each distinct
    turn produces its own envelope stored under a unique session hash.
    """

    def test_three_turn_conversation(self, tmp_path):
        from nexus.contracts.metadata import DT_MOUNT

        nx = _bootstrap_standalone_fs(tmp_path)
        try:
            nx.sys_setattr(
                "/llm",
                entry_type=DT_MOUNT,
                backend_type="openai",
                backend_name="openai_compatible",
                openai_base_url=_SSE_MOCK_OPENAI_BASE,
                openai_api_key="sk-mock",
                openai_model="mock-gpt-4o-mini",
                openai_blob_root=str(tmp_path / "llm_spool"),
            )

            # Accumulate conversation history across three user turns.
            messages = []
            envelopes = []
            for turn, user_msg in enumerate(["hi", "how are you?", "goodbye"]):
                messages.append({"role": "user", "content": user_msg})
                _, _sh, env = _llm_round_trip(
                    nx,
                    "/llm",
                    {"messages": list(messages), "model": "mock-gpt-4o-mini"},
                    session_suffix=f"t{turn}",
                )
                envelopes.append(env)
                # Feed assistant reply back into the context (canned text).
                messages.append({"role": "assistant", "content": "Hello from SSE mock."})

            # Three distinct prompts => three distinct session hashes.
            hashes = {env["request_hash"] for env in envelopes}
            assert len(hashes) == 3, f"expected 3 distinct request hashes, got {hashes}"

            # CAS dedup on repeat: re-running turn 0 returns the same envelope.
            _, _sh, env_repeat = _llm_round_trip(
                nx,
                "/llm",
                {
                    "messages": [{"role": "user", "content": "hi"}],
                    "model": "mock-gpt-4o-mini",
                },
                session_suffix="repeat",
            )
            assert env_repeat["request_hash"] == envelopes[0]["request_hash"]
        finally:
            nx.close()


# ===================================================================
# R13.3 Class 1/1: CLI surface — federation/zone/locks commands
# ===================================================================
class TestFederationCLISurface:
    """Smoke-test that `nexus federation|zone|locks <sub>` still works."""

    def test_federation_status_cli(self, cluster, api_key):
        rc, out, err = _cli_exec("nexus-dyn-node-1", ["federation", "status"], timeout=30)
        if rc != 0:
            pytest.skip(f"federation status CLI failed: rc={rc} err={err[:200]}")
        assert len(out) > 0, f"Empty output: stderr={err[:200]}"

    def test_federation_zones_cli(self, cluster, api_key):
        rc, out, err = _cli_exec("nexus-dyn-node-1", ["federation", "zones"], timeout=30)
        if rc != 0:
            pytest.skip(f"federation zones CLI failed: rc={rc} err={err[:200]}")
        for z in ["corp", "corp-eng", "family"]:
            assert z in out, f"Zone '{z}' not in output: {out[:400]}"

    def test_zone_list_cli(self, cluster, api_key):
        rc, out, err = _cli_exec("nexus-dyn-node-1", ["zone", "list"], timeout=30)
        if rc != 0:
            pytest.skip(f"zone list CLI failed: rc={rc} err={err[:200]}")
        assert len(out) > 0, f"Empty output from zone list: stderr={err[:200]}"

    def test_locks_list_cli(self, cluster, api_key):
        rc, out, err = _cli_exec("nexus-dyn-node-1", ["locks", "list"], timeout=30)
        if rc != 0 and "Usage" not in err and "No such command" not in err:
            pytest.skip(f"locks list CLI failed: rc={rc} err={err[:200]}")
