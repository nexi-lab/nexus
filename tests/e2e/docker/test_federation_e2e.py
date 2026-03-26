"""Federation E2E tests — long workflow scenarios against Docker cluster.

Tests the multi-zone topology:
  /              (root zone — personal workspace)
  /workspace/    (root zone)
  /corp/         → DT_MOUNT → zone "corp"
  /corp/engineering/ → DT_MOUNT → zone "corp-eng" (nested!)
  /corp/sales/   → DT_MOUNT → zone "corp-sales"
  /family/       → DT_MOUNT → zone "family"
  /family/work/  → DT_MOUNT → zone "corp" (cross-link)

5 zones: root, corp, corp-eng, corp-sales, family

Transport: gRPC Call RPC only (zero HTTP for business logic).
HTTP kept only for K8s health probes.

Run (from inside Docker network — production-consistent):
    docker compose -f dockerfiles/docker-compose.cross-platform-test.yml up -d
    docker compose -f dockerfiles/docker-compose.cross-platform-test.yml logs -f test
"""

import base64
import hashlib
import re
import struct
import subprocess
import time
import uuid

import grpc
import httpx
import pytest

# All tests share one Docker cluster — run sequentially in a single xdist worker.
pytestmark = [pytest.mark.xdist_group("federation-e2e")]

# ---------------------------------------------------------------------------
# Configuration — Docker-internal addresses (same network as Raft nodes)
# ---------------------------------------------------------------------------
NODE1_URL = "http://nexus-1:2026"
NODE2_URL = "http://nexus-2:2026"
NODE1_GRPC = "nexus-1:2028"
NODE2_GRPC = "nexus-2:2028"
HEALTH_TIMEOUT = 120  # longer for multi-zone startup


# Map hostname-derived Raft node IDs to gRPC targets (for leader-hint following)
def _hostname_to_node_id(hostname: str) -> int:
    """SHA-256 hostname → u64 (matches Rust/Python PeerAddress)."""
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
    """Send gRPC Call RPC, following Raft leader hints (up to 2 redirects)."""
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
                match = _LEADER_HINT_RE.search(str(result["message"]))
                if match:
                    leader_id = int(match.group(1))
                    leader_target = _NODE_ID_TO_GRPC.get(leader_id)
                    if leader_target and leader_target != current:
                        current = leader_target
                        continue
            if resp.is_error:
                return {"error": result}
            return result  # Already {"result": <data>} from servicer
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
    """gRPC Call wrapper that calls pytest.skip() on unavailable methods."""
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
    """Check /health endpoint. Returns None if unreachable."""
    try:
        resp = httpx.get(f"{url}/health", timeout=5, trust_env=False)
        if resp.status_code == 200:
            return resp.json()
    except httpx.TransportError:
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


# Deterministic admin key set via NEXUS_API_KEY in docker-compose.cross-platform-test.yml.
# The entrypoint registers this key in the database on startup — no runtime creation needed.
E2E_ADMIN_API_KEY = "sk-test-federation-e2e-admin-key"


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
    if isinstance(data, bytes):
        return data.decode()
    if isinstance(data, str):
        return data
    return str(data)


def _list_paths(result: dict) -> list[str]:
    """Extract list of paths from a list response."""
    files = result["result"]["files"]
    return [f["path"] if isinstance(f, dict) else f for f in files]


def _wait_replicated(
    target: str,
    parent: str,
    expected_path: str,
    api_key: str,
    *,
    msg: str = "File not replicated",
    timeout: float = 10,
) -> None:
    """Poll list on a node until expected_path appears (Raft replication lag)."""
    deadline = time.time() + timeout
    while True:
        ls = _grpc_call(target, "list", {"path": parent}, api_key=api_key, timeout=5)
        if "error" not in ls and expected_path in _list_paths(ls):
            return
        if time.time() >= deadline:
            pytest.fail(f"{msg}: {expected_path} not in {parent} on {target}")
        time.sleep(0.5)


def _wait_content_replicated(
    target: str,
    path: str,
    expected_content: str,
    api_key: str,
    *,
    timeout: float = 15,
) -> None:
    """Poll read until content matches expected (not just listing)."""
    deadline = time.time() + timeout
    while True:
        r = _grpc_call(target, "read", {"path": path}, api_key=api_key, timeout=5)
        if "error" not in r:
            try:
                if _decode_content(r) == expected_content:
                    return
            except Exception:
                pass
        if time.time() >= deadline:
            pytest.fail(f"Content not replicated: {path} on {target}")
        time.sleep(0.5)


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
            "Raft cluster not reachable. Ensure this test runs inside the Docker network:\n"
            "  docker compose -f dockerfiles/docker-compose.cross-platform-test.yml up -d"
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
    """Admin API key pre-registered via NEXUS_API_KEY in docker-compose."""
    return E2E_ADMIN_API_KEY


# ---------------------------------------------------------------------------
# Class 1: Distributed Team Workday
# ---------------------------------------------------------------------------
class TestDistributedTeamWorkday:
    """Engineer's full workday across all 5 federated zones.

    Covers: write, read, mkdir, list, glob, grep, sys_stat, exists,
    is_directory, rename, copy, delete, cross-link, zone isolation,
    cross-node replication, health, healthz, health/detailed
    """

    def test_full_workday(self, cluster, api_key):
        uid = _uid()
        node1 = cluster["node1"]
        node2 = cluster["node2"]
        grpc1 = cluster["grpc1"]
        grpc2 = cluster["grpc2"]

        # --- Step 1: Both nodes healthy (HTTP — K8s probes) ---
        for url in [node1, node2]:
            h = _health(url)
            assert h is not None, f"{url} unreachable"
            assert h["status"] == "healthy"

        # --- Step 2: K8s probes alive (HTTP) ---
        for probe in ["/healthz/live", "/healthz/ready", "/healthz/startup"]:
            resp = httpx.get(f"{node1}{probe}", timeout=5, trust_env=False)
            assert resp.status_code == 200, f"Probe {probe} failed: {resp.status_code}"

        # --- Step 3: Detailed health check (HTTP — admin) ---
        resp = httpx.get(
            f"{node1}/health/detailed",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5,
            trust_env=False,
        )
        if resp.status_code == 200:
            data = resp.json()
            assert isinstance(data, dict)

        # --- Step 4: Create project dir in corp-eng ---
        project_dir = f"/corp/engineering/project-{uid}"
        mk = _grpc_call(grpc1, "mkdir", {"path": project_dir, "parents": True}, api_key=api_key)
        assert "error" not in mk, f"mkdir failed: {mk}"

        # --- Step 5: Write README, main.py, test.py ---
        readme = f"{project_dir}/README.md"
        main_py = f"{project_dir}/main.py"
        test_py = f"{project_dir}/test.py"

        main_py_content = (
            f"def main():\n    print('hello {uid}')\n\nif __name__ == '__main__':\n    main()\n"
        )

        for path, content in [
            (readme, f"# Project {uid}\nA test project."),
            (main_py, main_py_content),
            (test_py, f"def test_main():\n    assert True  # {uid}\n"),
        ]:
            w = _grpc_call(grpc1, "write", {"path": path, "content": content}, api_key=api_key)
            assert "error" not in w, f"Write {path} failed: {w}"

        # --- Step 6: Personal todo in root zone ---
        todo = f"/workspace/todo-{uid}.md"
        w = _grpc_call(
            grpc1,
            "write",
            {"path": todo, "content": f"# TODO {uid}\n- Ship feature"},
            api_key=api_key,
        )
        assert "error" not in w

        # --- Step 7: Sales proposal in corp-sales ---
        proposal = f"/corp/sales/proposal-{uid}.pdf"
        w = _grpc_call(
            grpc1, "write", {"path": proposal, "content": f"proposal-{uid}"}, api_key=api_key
        )
        assert "error" not in w

        # --- Step 8: Family photo in family zone ---
        photo = f"/family/photo-{uid}.jpg"
        w = _grpc_call(grpc1, "write", {"path": photo, "content": f"photo-{uid}"}, api_key=api_key)
        assert "error" not in w

        # --- Step 9: List project dir → 3 files ---
        ls = _grpc_call(grpc1, "list", {"path": f"{project_dir}/"}, api_key=api_key)
        assert "error" not in ls, f"List failed: {ls}"
        paths = _list_paths(ls)
        assert len(paths) >= 3, f"Expected 3+ files, got: {paths}"
        assert readme in paths
        assert main_py in paths
        assert test_py in paths

        # --- Step 10: Glob *.py in corp-eng ---
        g = _grpc_call(grpc1, "glob", {"pattern": f"{project_dir}/*.py"}, api_key=api_key)
        assert "error" not in g, f"Glob failed: {g}"
        matches = g["result"]
        if isinstance(matches, dict):
            matches = matches.get("matches", matches.get("files", []))
        assert len(matches) >= 2, f"Expected 2+ .py matches, got: {matches}"

        # --- Step 11: Grep for function def ---
        grep_r = _grpc_call(
            grpc1, "grep", {"pattern": "def main", "path": project_dir}, api_key=api_key
        )
        assert "error" not in grep_r, f"Grep failed: {grep_r}"
        grep_results = grep_r["result"]
        if isinstance(grep_results, dict):
            grep_results = grep_results.get("results", [])
        assert len(grep_results) >= 1, f"Expected grep hit, got: {grep_results}"

        # --- Step 12: Get metadata on README (sys_stat — kernel syscall) ---
        m = _grpc_call(grpc1, "sys_stat", {"path": readme}, api_key=api_key)
        assert "error" not in m, f"sys_stat failed: {m}"
        meta = m["result"]
        if isinstance(meta, dict) and "metadata" in meta:
            meta = meta["metadata"]
        if isinstance(meta, dict):
            assert meta.get("path") == readme

        # --- Step 13: Exists check (true) ---
        ex = _grpc_call(grpc1, "exists", {"path": readme}, api_key=api_key)
        assert "error" not in ex
        exists_val = ex["result"]
        if isinstance(exists_val, dict):
            exists_val = exists_val.get("exists", exists_val)
        assert exists_val is True

        # --- Step 14: Is directory check ---
        isdir = _grpc_call(grpc1, "is_directory", {"path": project_dir}, api_key=api_key)
        assert "error" not in isdir, f"is_directory failed: {isdir}"
        isdir_val = isdir["result"]
        if isinstance(isdir_val, dict):
            isdir_val = isdir_val.get("is_directory", isdir_val)
        assert isdir_val is True

        # --- Step 15: Cross-link: read corp file via /family/work/ ---
        # /family/work/ cross-links to the "corp" zone, so /corp/X is /family/work/X.
        corp_memo = f"/corp/memo-{uid}.txt"
        w = _grpc_call(
            grpc1, "write", {"path": corp_memo, "content": f"memo-{uid}"}, api_key=api_key
        )
        assert "error" not in w
        crosslink_path = f"/family/work/memo-{uid}.txt"
        r = _grpc_call(grpc1, "read", {"path": crosslink_path}, api_key=api_key)
        assert "error" not in r, f"Cross-link read failed: {r}"
        assert _decode_content(r) == f"memo-{uid}"

        # --- Step 16: Cross-link: write via /family/work/, read via /corp/ ---
        crosslink_write = f"/family/work/crosslink-{uid}.txt"
        w = _grpc_call(
            grpc1,
            "write",
            {"path": crosslink_write, "content": f"crosslink-{uid}"},
            api_key=api_key,
        )
        assert "error" not in w
        direct_read = f"/corp/crosslink-{uid}.txt"
        r = _grpc_call(grpc1, "read", {"path": direct_read}, api_key=api_key)
        assert "error" not in r, f"Cross-link reverse read failed: {r}"
        assert _decode_content(r) == f"crosslink-{uid}"

        # --- Step 17: Zone isolation: family file NOT in corp listing ---
        ls_family = _grpc_call(grpc1, "list", {"path": "/family/"}, api_key=api_key)
        assert "error" not in ls_family
        family_paths = _list_paths(ls_family)
        assert photo in family_paths

        ls_corp = _grpc_call(grpc1, "list", {"path": "/corp/"}, api_key=api_key)
        assert "error" not in ls_corp
        corp_paths = _list_paths(ls_corp)
        assert photo not in corp_paths, "Family file leaked into corp zone!"

        # --- Step 18: Rename file within corp-eng ---
        renamed_path = f"{project_dir}/test_renamed-{uid}.py"
        rn = _grpc_call(
            grpc1, "rename", {"old_path": test_py, "new_path": renamed_path}, api_key=api_key
        )
        assert "error" not in rn, f"Rename failed: {rn}"

        # --- Step 19: Read renamed file (content preserved) ---
        r = _grpc_call(grpc1, "read", {"path": renamed_path}, api_key=api_key)
        assert "error" not in r, f"Read renamed failed: {r}"
        assert f"# {uid}" in _decode_content(r)

        # --- Step 20: Copy file from corp-eng to corp-sales ---
        copy_dst = f"/corp/sales/copied-readme-{uid}.md"
        cp = _grpc_call(grpc1, "copy", {"src_path": readme, "dst_path": copy_dst}, api_key=api_key)
        if "error" in cp:
            # Copy may not support cross-zone — try within same zone
            copy_dst_same = f"{project_dir}/copied-readme-{uid}.md"
            cp = _grpc_call(
                grpc1, "copy", {"src_path": readme, "dst_path": copy_dst_same}, api_key=api_key
            )
            copy_dst = None if "error" in cp else copy_dst_same

        # --- Step 21: Read copied file ---
        if copy_dst:
            r = _grpc_call(grpc1, "read", {"path": copy_dst}, api_key=api_key)
            assert "error" not in r, f"Read copied file failed: {r}"
            assert f"Project {uid}" in _decode_content(r)

        # --- Step 22: Cross-node: verify all files on node-2 ---
        files_to_replicate = [
            (readme, f"{project_dir}/"),
            (main_py, f"{project_dir}/"),
            (todo, "/workspace/"),
            (proposal, "/corp/sales/"),
            (photo, "/family/"),
            (corp_memo, "/corp/"),
        ]
        for filepath, parent in files_to_replicate:
            _wait_replicated(
                grpc2,
                parent,
                filepath,
                api_key,
                msg=f"Not replicated to node-2: {filepath}",
            )

        # --- Step 23: Read content from node-2 ---
        r = _grpc_call(grpc2, "read", {"path": readme}, api_key=api_key)
        assert "error" not in r, f"Cross-node read failed: {r}"
        assert f"Project {uid}" in _decode_content(r)

        # --- Step 24: Delete test file ---
        d = _grpc_call(grpc1, "delete", {"path": corp_memo}, api_key=api_key)
        assert "error" not in d, f"Delete failed: {d}"

        # --- Step 25: Exists → false (try both nodes for Raft consistency) ---
        for _retry in range(10):
            # Check both nodes — leader has the committed state
            for grpc_target in [grpc1, grpc2]:
                ex = _grpc_call(grpc_target, "exists", {"path": corp_memo}, api_key=api_key)
                if "error" in ex:
                    continue
                exists_val = ex["result"]
                if isinstance(exists_val, dict):
                    exists_val = exists_val.get("exists", exists_val)
                if exists_val is False:
                    break
            if exists_val is False:
                break
            time.sleep(0.5)
        assert exists_val is False, f"File still exists after delete + {_retry * 0.5}s"

        # --- Step 26: List → file gone (check leader node) ---
        for grpc_target in [grpc1, grpc2]:
            ls = _grpc_call(grpc_target, "list", {"path": "/corp/"}, api_key=api_key)
            if "error" not in ls and corp_memo not in _list_paths(ls):
                break
        assert "error" not in ls
        assert corp_memo not in _list_paths(ls)


# ---------------------------------------------------------------------------
# Class 2: Federation Admin Introspection
# ---------------------------------------------------------------------------
class TestFederationAdminIntrospection:
    """Admin inspects federation topology, cluster health, and observability stack.

    Covers: federation_list_zones, federation_cluster_info, health/detailed,
    events_replay, audit_list
    """

    def test_admin_introspection(self, cluster, api_key):
        node1 = cluster["node1"]
        node2 = cluster["node2"]
        grpc1 = cluster["grpc1"]
        grpc2 = cluster["grpc2"]
        uid = _uid()

        # --- Step 1: List all zones ---
        zones_r = _grpc_call(grpc1, "federation_list_zones", {}, api_key=api_key)
        assert "error" not in zones_r, f"federation_list_zones failed: {zones_r}"
        zones = zones_r["result"]["zones"]
        zone_ids = [z["zone_id"] for z in zones]
        for expected in ["root", "corp", "corp-eng", "corp-sales", "family"]:
            assert expected in zone_ids, f"Zone {expected} missing from {zone_ids}"

        # --- Step 2: Cluster info for each zone ---
        for zone_id in zone_ids:
            info = _grpc_call(
                grpc1, "federation_cluster_info", {"zone_id": zone_id}, api_key=api_key
            )
            assert "error" not in info, f"cluster_info({zone_id}) failed: {info}"
            assert info["result"]["zone_id"] == zone_id

        # --- Step 3: Verify corp zone dual-mounted ---
        corp_info = _grpc_call(
            grpc1, "federation_cluster_info", {"zone_id": "corp"}, api_key=api_key
        )
        assert "error" not in corp_info
        assert corp_info["result"]["links_count"] >= 2, (
            f"Corp zone should have ≥2 links (/corp/ + /family/work/), "
            f"got {corp_info['result']['links_count']}"
        )

        # --- Step 4-5: Detailed health on both nodes (HTTP — admin health) ---
        for url in [node1, node2]:
            resp = httpx.get(
                f"{url}/health/detailed",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=5,
                trust_env=False,
            )
            if resp.status_code == 200:
                data = resp.json()
                assert isinstance(data, dict)

        # --- Step 6: Write 2 files (seed activity for events) ---
        for i in range(2):
            path = f"/workspace/admin-seed-{uid}-{i}.txt"
            w = _grpc_call(
                grpc1, "write", {"path": path, "content": f"seed-{uid}-{i}"}, api_key=api_key
            )
            assert "error" not in w

        # --- Step 7: Events replay (gRPC) ---
        events_r = _grpc_call_or_skip(
            grpc1,
            "events_replay",
            {"limit": 10},
            api_key=api_key,
            skip_msg="Events replay not available",
        )
        if "error" not in events_r:
            result = events_r.get("result", events_r)
            if isinstance(result, dict):
                assert "events" in result

        # --- Step 8: Audit list (gRPC) ---
        audit_r = _grpc_call_or_skip(
            grpc1,
            "audit_list",
            {},
            api_key=api_key,
            skip_msg="Audit list not available",
        )
        # Just verify parseable — no assertion on content
        assert isinstance(audit_r, dict)

        # --- Step 9: Federation topology consistent on node-2 ---
        zones_n2 = _grpc_call(grpc2, "federation_list_zones", {}, api_key=api_key)
        assert "error" not in zones_n2, f"federation_list_zones on node-2 failed: {zones_n2}"
        zone_ids_n2 = [z["zone_id"] for z in zones_n2["result"]["zones"]]
        assert len(zone_ids_n2) == len(zone_ids), (
            f"Zone count mismatch: node-1={len(zone_ids)}, node-2={len(zone_ids_n2)}"
        )


# ---------------------------------------------------------------------------
# Class 3: Snapshot Atomic Operations
# ---------------------------------------------------------------------------
class TestSnapshotAtomicOperations:
    """Developer uses transactional snapshots for safe multi-file atomic changes.

    Covers: snapshots (create, get, list, entries, commit, rollback)
    """

    def test_snapshot_workflow(self, cluster, api_key):
        uid = _uid()
        grpc1 = cluster["grpc1"]

        # --- Step 1-2: Write baseline files ---
        file_a = f"/corp/snap-{uid}-a.txt"
        file_b = f"/corp/snap-{uid}-b.txt"
        for path, content in [(file_a, f"baseline-a-{uid}"), (file_b, f"baseline-b-{uid}")]:
            w = _grpc_call(grpc1, "write", {"path": path, "content": content}, api_key=api_key)
            assert "error" not in w, f"Write {path} failed: {w}"

        # --- Step 3: Begin transaction — skip entire test if unavailable ---
        begin_r = _grpc_call_or_skip(
            grpc1,
            "snapshot_create",
            {"description": f"E2E commit {uid}", "ttl_seconds": 3600},
            api_key=api_key,
            skip_msg="Snapshot API not available",
        )
        if "error" in begin_r:
            pytest.skip(f"snapshot_create returned error: {begin_r}")
        txn_data = begin_r.get("result", begin_r)
        txn_id = txn_data["transaction_id"]

        # --- Step 4-5: Get transaction status ---
        status_r = _grpc_call(grpc1, "snapshot_get", {"transaction_id": txn_id}, api_key=api_key)
        assert "error" not in status_r, f"snapshot_get failed: {status_r}"
        status_data = status_r.get("result", status_r)
        assert "status" in status_data

        # --- Step 6: List transactions — our txn in list ---
        list_r = _grpc_call(grpc1, "snapshot_list", {}, api_key=api_key)
        assert "error" not in list_r, f"snapshot_list failed: {list_r}"
        list_data = list_r.get("result", list_r)
        txn_ids_in_list = [
            t["transaction_id"] for t in list_data.get("transactions", list_data.get("items", []))
        ]
        assert txn_id in txn_ids_in_list, f"Txn {txn_id} not in list: {txn_ids_in_list}"

        # --- Step 7: Write file C ---
        file_c = f"/corp/snap-{uid}-c.txt"
        w = _grpc_call(grpc1, "write", {"path": file_c, "content": f"new-c-{uid}"}, api_key=api_key)
        assert "error" not in w

        # --- Step 8: List entries ---
        entries_r = _grpc_call(
            grpc1, "snapshot_list_entries", {"transaction_id": txn_id}, api_key=api_key
        )
        if "error" not in entries_r:
            entries_data = entries_r.get("result", entries_r)
            assert isinstance(entries_data, dict)

        # --- Step 9: Commit ---
        commit_r = _grpc_call(grpc1, "snapshot_commit", {"transaction_id": txn_id}, api_key=api_key)
        assert "error" not in commit_r, f"Commit failed: {commit_r}"

        # --- Step 10: Read file C → exists ---
        r = _grpc_call(grpc1, "read", {"path": file_c}, api_key=api_key)
        assert "error" not in r, f"Read file C after commit failed: {r}"
        assert _decode_content(r) == f"new-c-{uid}"

        # --- Step 11: Begin 2nd transaction (rollback) ---
        begin2_r = _grpc_call(
            grpc1,
            "snapshot_create",
            {"description": f"E2E rollback {uid}", "ttl_seconds": 3600},
            api_key=api_key,
        )
        assert "error" not in begin2_r, f"Begin txn2 failed: {begin2_r}"
        txn2_data = begin2_r.get("result", begin2_r)
        txn2_id = txn2_data["transaction_id"]

        # --- Step 12: Write file D ---
        file_d = f"/corp/snap-{uid}-d.txt"
        w = _grpc_call(
            grpc1, "write", {"path": file_d, "content": f"temp-d-{uid}"}, api_key=api_key
        )
        assert "error" not in w

        # --- Step 13: Rollback ---
        rollback_r = _grpc_call(grpc1, "snapshot_restore", {"txn_id": txn2_id}, api_key=api_key)
        assert "error" not in rollback_r, f"Rollback failed: {rollback_r}"

        # --- Step 14: Get rolled-back txn status ---
        final_r = _grpc_call(grpc1, "snapshot_get", {"transaction_id": txn2_id}, api_key=api_key)
        assert "error" not in final_r
        final_data = final_r.get("result", final_r)
        final_status = final_data.get("status", "")
        assert final_status != "active", f"Expected non-active status, got: {final_status}"


# ---------------------------------------------------------------------------
# Class 4: Distributed Lock Coordination
# ---------------------------------------------------------------------------
class TestDistributedLockCoordination:
    """Two nodes coordinate on shared resources via distributed locks.

    Covers: locks (acquire, info, extend, list, release), cross-node lock visibility
    """

    def test_lock_coordination(self, cluster, api_key):
        uid = _uid()
        grpc1 = cluster["grpc1"]
        grpc2 = cluster["grpc2"]

        # --- Step 1: Write target file ---
        locked_file = f"/corp/engineering/locked-{uid}.txt"
        w = _grpc_call(
            grpc1, "write", {"path": locked_file, "content": f"lock-{uid}"}, api_key=api_key
        )
        assert "error" not in w

        # --- Step 2: Acquire lock on node-1 — skip entire test if unavailable ---
        acquire_r = _grpc_call_or_skip(
            grpc1,
            "lock_acquire",
            {"path": locked_file, "ttl": 60},
            api_key=api_key,
            skip_msg="Lock API not available",
        )
        if "error" in acquire_r:
            pytest.skip(f"lock_acquire returned error: {acquire_r}")
        lock_data = acquire_r.get("result", acquire_r)
        assert lock_data.get("acquired") is True, f"Lock not acquired: {lock_data}"

        # --- Step 3: Record lock_id ---
        lock_id = lock_data.get("lock_id", "")
        assert lock_id, f"No lock_id in response: {lock_data}"

        # --- Step 4: Check lock status node-1 ---
        info1 = _grpc_call(grpc1, "lock_info", {"path": locked_file}, api_key=api_key)
        assert "error" not in info1, f"lock_info failed: {info1}"
        info1_data = info1.get("result", info1)
        assert info1_data.get("locked") is True, f"Expected locked=True, got: {info1_data}"

        # --- Step 5: Check lock status node-2 ---
        info2 = _grpc_call(grpc2, "lock_info", {"path": locked_file}, api_key=api_key)
        assert "error" not in info2, f"lock_info on node-2 failed: {info2}"
        info2_data = info2.get("result", info2)
        assert info2_data.get("locked") is True, f"Lock not visible on node-2: {info2_data}"

        # --- Step 6: Extend TTL ---
        extend_r = _grpc_call(
            grpc1,
            "lock_extend",
            {"lock_id": lock_id, "path": locked_file, "ttl": 120},
            api_key=api_key,
        )
        assert "error" not in extend_r, f"Extend failed: {extend_r}"
        extend_data = extend_r.get("result", extend_r)
        assert extend_data.get("success") is True

        # --- Step 7: List active locks ---
        list_r = _grpc_call(grpc1, "lock_list", {}, api_key=api_key)
        assert "error" not in list_r, f"lock_list failed: {list_r}"
        list_data = list_r.get("result", list_r)
        lock_paths = [
            lk.get("path", "") for lk in list_data.get("locks", list_data.get("items", []))
        ]
        assert locked_file in lock_paths, f"Our lock not in list: {lock_paths}"

        # --- Step 8: Release lock ---
        release_r = _grpc_call(
            grpc1,
            "lock_release",
            {"path": locked_file, "lock_id": lock_id},
            api_key=api_key,
        )
        assert "error" not in release_r, f"Release failed: {release_r}"
        release_data = release_r.get("result", release_r)
        assert release_data.get("released") is True

        # --- Step 9: Verify released ---
        info3 = _grpc_call(grpc1, "lock_info", {"path": locked_file}, api_key=api_key)
        if "error" not in info3:
            info3_data = info3.get("result", info3)
            assert info3_data.get("locked") is False or info3_data.get("locked") is None, (
                f"Lock still active after release: {info3_data}"
            )

        # --- Step 10: Re-acquire from node-2 ---
        acquire2_r = _grpc_call(
            grpc2,
            "lock_acquire",
            {"path": locked_file, "ttl": 60},
            api_key=api_key,
        )
        assert "error" not in acquire2_r, f"Re-acquire on node-2 failed: {acquire2_r}"
        lock2_data = acquire2_r.get("result", acquire2_r)
        lock2_id = lock2_data.get("lock_id", "")

        # --- Step 11: Release from node-2 (cleanup) ---
        release2_r = _grpc_call(
            grpc2,
            "lock_release",
            {"path": locked_file, "lock_id": lock2_id},
            api_key=api_key,
        )
        assert "error" not in release2_r, f"Release from node-2 failed: {release2_r}"


# ---------------------------------------------------------------------------
# Class 5: Leader Failover and Recovery (LAST — restarts containers)
# ---------------------------------------------------------------------------
class TestLeaderFailoverAndRecovery:
    """Leader crash, survivor takes over, reads all data, writes new data, leader recovers.

    Covers: failover, federation content read, leader re-election,
    catch-up replication, topology consistency
    """

    def test_failover_and_recovery(self, cluster, api_key):
        # Requires Docker CLI inside test container (docker stop/start)
        import shutil

        if shutil.which("docker") is None:
            pytest.skip("Docker CLI not available in test container")

        uid = _uid()
        node1 = cluster["node1"]
        node2 = cluster["node2"]
        grpc1 = cluster["grpc1"]
        grpc2 = cluster["grpc2"]

        # --- Step 1: Write file in each zone (5 zones) ---
        zone_files: dict[str, tuple[str, str, str]] = {}  # zone → (path, parent, content)
        for zone, prefix, parent in [
            ("root", "/workspace/", "/workspace/"),
            ("corp", "/corp/", "/corp/"),
            ("corp-eng", "/corp/engineering/", "/corp/engineering/"),
            ("corp-sales", "/corp/sales/", "/corp/sales/"),
            ("family", "/family/", "/family/"),
        ]:
            path = f"{prefix}failover-{uid}-{zone}.txt"
            content = f"failover-{zone}-{uid}"
            w = _grpc_call(grpc1, "write", {"path": path, "content": content}, api_key=api_key)
            assert "error" not in w, f"Write to {zone} failed: {w}"
            zone_files[zone] = (path, parent, content)

        # --- Step 2: Write large file (>4KB) to exercise streaming path ---
        large_content = f"LARGE-{uid}\n" + ("x" * 5000) + f"\nEND-{uid}"
        large_path = f"/corp/engineering/large-{uid}.bin"
        w = _grpc_call(
            grpc1, "write", {"path": large_path, "content": large_content}, api_key=api_key
        )
        assert "error" not in w, f"Large file write failed: {w}"

        # --- Step 3: Wait for replication ---
        for zone, (path, parent, _content) in zone_files.items():
            _wait_replicated(
                grpc2,
                parent,
                path,
                api_key,
                msg=f"{zone} file not replicated before failover",
                timeout=15,
            )
        _wait_replicated(
            grpc2,
            "/corp/engineering/",
            large_path,
            api_key,
            msg="Large file not replicated before failover",
            timeout=15,
        )

        # --- Step 4: Verify content on node-2 before failover ---
        for zone, (path, _parent, content) in zone_files.items():
            r = _grpc_call(grpc2, "read", {"path": path}, api_key=api_key)
            assert "error" not in r, f"Pre-failover read {zone} on node-2 failed: {r}"
            assert _decode_content(r) == content

        # --- Step 5: Stop node-1 ---
        subprocess.run(["docker", "stop", "nexus-node-1"], timeout=30, check=True)

        try:
            # --- Step 6: Wait for node-2 healthy (HTTP — K8s probe) ---
            _wait_healthy([node2], timeout=30)

            # --- Step 7: Read ALL 5 files from node-2 ---
            for zone, (path, _parent, content) in zone_files.items():
                r = _grpc_call(grpc2, "read", {"path": path}, api_key=api_key, timeout=15)
                assert "error" not in r, f"Failover read ({zone}) failed: {r}"
                assert _decode_content(r) == content, (
                    f"Content mismatch for {zone}: expected={content!r}"
                )

            # --- Step 8: Verify large file integrity ---
            r = _grpc_call(grpc2, "read", {"path": large_path}, api_key=api_key, timeout=15)
            assert "error" not in r, f"Failover read (large file) failed: {r}"
            assert _decode_content(r) == large_content

            # --- Step 9: Write 2 new files on node-2 (new leader) ---
            new_files = []
            for i in range(2):
                path = f"/corp/engineering/post-failover-{uid}-{i}.txt"
                content = f"post-failover-{uid}-{i}"
                w = _grpc_call(grpc2, "write", {"path": path, "content": content}, api_key=api_key)
                assert "error" not in w, f"Post-failover write {i} failed: {w}"
                new_files.append((path, content))

        finally:
            # --- Step 10: Restart node-1 ---
            subprocess.run(["docker", "start", "nexus-node-1"], timeout=30, check=True)

            # --- Step 11: Wait for node-1 healthy (HTTP — K8s probe) ---
            _wait_healthy([node1], timeout=60)

        # --- Step 12: Node-1 catches up: new files readable ---
        for path, content in new_files:
            _wait_content_replicated(grpc1, path, content, api_key, timeout=20)

        # --- Step 13: Topology verification ---
        for target in [grpc1, grpc2]:
            zones_r = _grpc_call(target, "federation_list_zones", {}, api_key=api_key)
            assert "error" not in zones_r, f"federation_list_zones failed on {target}: {zones_r}"
            zone_ids = [z["zone_id"] for z in zones_r["result"]["zones"]]
            for expected in ["root", "corp", "corp-eng", "corp-sales", "family"]:
                assert expected in zone_ids, f"Zone {expected} missing on {target}: {zone_ids}"

        # --- Step 14: Both nodes healthy (HTTP — K8s probe) ---
        for url in [node1, node2]:
            h = _health(url)
            assert h is not None, f"{url} not healthy after recovery"
            assert h["status"] == "healthy"
