"""Real E2E tests for federation CLI — actual Rust gRPC server, no mocking.

Starts a real ZoneManager (PyO3 → Tokio runtime → gRPC server), bootstraps
Raft zones, then tests all CLI commands against the live server.

Stack exercised: Click CLI → gRPC client → real gRPC server → real Raft consensus.

Requirements:
    maturin develop -m rust/nexus_raft/Cargo.toml --features full
"""

import asyncio
import json
import socket
import tempfile
import time
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

# Skip entire module if PyO3 bindings are not built with ZoneManager
try:
    from _nexus_raft import ZoneManager as PyZoneManager  # type: ignore[import-untyped]  # allowed
except (ImportError, AttributeError):
    pytest.skip("Requires maturin build with --features full", allow_module_level=True)

from nexus.cli.commands.federation import (
    _parse_peer_spec,
    federation,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    """Find a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_leader(addr: str, zone_id: str, timeout: float = 10.0) -> dict:
    """Poll GetClusterInfo via inline gRPC until a leader is elected or timeout."""
    from grpc import aio as grpc_aio

    from nexus.raft import transport_pb2, transport_pb2_grpc

    async def _poll() -> dict:
        channel = grpc_aio.insecure_channel(addr)
        try:
            stub = transport_pb2_grpc.ZoneApiServiceStub(channel)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    resp = await stub.GetClusterInfo(
                        transport_pb2.GetClusterInfoRequest(zone_id=zone_id),
                        timeout=5.0,
                    )
                    if resp.leader_id > 0:
                        return {
                            "node_id": resp.node_id,
                            "leader_id": resp.leader_id,
                            "term": resp.term,
                            "is_leader": resp.is_leader,
                            "leader_address": resp.leader_address or None,
                            "applied_index": resp.applied_index,
                        }
                except Exception:
                    pass
                await asyncio.sleep(0.3)
            raise TimeoutError(f"No leader elected for zone '{zone_id}' within {timeout}s")
        finally:
            await channel.close()

    return asyncio.run(_poll())


def _get_cluster_info(addr: str, zone_id: str) -> dict:
    """Get cluster info via inline gRPC (single call)."""
    from grpc import aio as grpc_aio

    from nexus.raft import transport_pb2, transport_pb2_grpc

    async def _query() -> dict:
        channel = grpc_aio.insecure_channel(addr)
        try:
            stub = transport_pb2_grpc.ZoneApiServiceStub(channel)
            resp = await stub.GetClusterInfo(
                transport_pb2.GetClusterInfoRequest(zone_id=zone_id),
                timeout=10.0,
            )
            return {
                "node_id": resp.node_id,
                "leader_id": resp.leader_id,
                "term": resp.term,
                "is_leader": resp.is_leader,
                "leader_address": resp.leader_address or None,
                "applied_index": resp.applied_index,
            }
        finally:
            await channel.close()

    return asyncio.run(_query())


def _create_zone_manager(node_id: int, base_path: str, bind_addr: str) -> "PyZoneManager":
    """Create a PyO3 ZoneManager directly (no auto-TLS, no Python wrapper overhead)."""
    return PyZoneManager(node_id, base_path, bind_addr)


# ---------------------------------------------------------------------------
# Fixtures — real Rust gRPC servers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def single_node_server():
    """Start a single-node Raft gRPC server with one bootstrapped zone.

    Uses PyO3 ZoneManager directly (no auto-TLS) for a plain gRPC server.
    The node self-elects as leader (single-node cluster).
    """
    tmpdir = tempfile.mkdtemp(prefix="nexus_e2e_fed_")
    port = _find_free_port()
    bind_addr = f"127.0.0.1:{port}"

    mgr = _create_zone_manager(node_id=1, base_path=tmpdir, bind_addr=bind_addr)

    # Bootstrap a zone — single node, self-elects as leader
    mgr.create_zone("test-zone", [])

    # Wait for leader election
    info = _wait_for_leader(bind_addr, "test-zone")
    assert info["is_leader"], "Single-node should self-elect as leader"

    yield {
        "mgr": mgr,
        "addr": bind_addr,
        "port": port,
        "data_dir": tmpdir,
        "zone_id": "test-zone",
        "leader_info": info,
    }

    mgr.shutdown()


@pytest.fixture(scope="module")
def two_node_cluster():
    """Start a 2-node Raft cluster for leader + follower profile testing.

    Node 1 and Node 2 form a 2-node cluster for zone 'cluster-zone'.
    One becomes leader, the other follower.
    """
    tmpdir1 = tempfile.mkdtemp(prefix="nexus_e2e_fed_n1_")
    tmpdir2 = tempfile.mkdtemp(prefix="nexus_e2e_fed_n2_")
    port1 = _find_free_port()
    port2 = _find_free_port()
    addr1 = f"127.0.0.1:{port1}"
    addr2 = f"127.0.0.1:{port2}"

    mgr1 = _create_zone_manager(node_id=1, base_path=tmpdir1, bind_addr=addr1)
    mgr2 = _create_zone_manager(node_id=2, base_path=tmpdir2, bind_addr=addr2)

    # Node 1 creates zone with Node 2 as peer
    mgr1.create_zone("cluster-zone", [f"2@{addr2}"])
    # Node 2 joins the zone with Node 1 as peer
    mgr2.join_zone("cluster-zone", [f"1@{addr1}"])

    # Wait for leader election
    info = _wait_for_leader(addr1, "cluster-zone", timeout=15.0)

    # Determine which node is leader and which is follower
    leader_addr = addr1
    follower_addr = addr2
    if not info.get("is_leader"):
        leader_addr, follower_addr = follower_addr, leader_addr

    yield {
        "mgr1": mgr1,
        "mgr2": mgr2,
        "addr1": addr1,
        "addr2": addr2,
        "data_dir1": tmpdir1,
        "data_dir2": tmpdir2,
        "leader_addr": leader_addr,
        "follower_addr": follower_addr,
        "zone_id": "cluster-zone",
    }

    mgr1.shutdown()
    mgr2.shutdown()


@pytest.fixture(autouse=True)
def _disable_auto_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable auto-JSON in CliRunner (stdout is not a TTY)."""
    monkeypatch.setenv("NEXUS_NO_AUTO_JSON", "1")


# ---------------------------------------------------------------------------
# federation status — single node (Leader profile)
# ---------------------------------------------------------------------------


class TestStatusLeader:
    """Test 'federation status' against a real leader node."""

    def test_status_rich_output(self, single_node_server: dict) -> None:
        srv = single_node_server
        runner = CliRunner()
        result = runner.invoke(
            federation,
            ["status", srv["zone_id"], "--addr", srv["addr"]],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert srv["zone_id"] in result.output
        assert "Leader" in result.output
        # Term should be present (exact value depends on election)
        assert "Term:" in result.output
        assert "Applied:" in result.output

    def test_status_json_output(self, single_node_server: dict) -> None:
        srv = single_node_server
        runner = CliRunner()
        result = runner.invoke(
            federation,
            ["status", srv["zone_id"], "--addr", srv["addr"], "--json"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["zone_id"] == srv["zone_id"]
        assert data["is_leader"] is True
        assert data["role"] == "Leader"
        assert data["node_id"] == 1
        assert data["leader_id"] == 1
        assert data["term"] > 0
        assert data["applied_index"] >= 0
        # leader_address may be None for single-node (no peers to track it)

    def test_status_nonexistent_zone(self, single_node_server: dict) -> None:
        srv = single_node_server
        runner = CliRunner()
        result = runner.invoke(
            federation,
            ["status", "nonexistent-zone", "--addr", srv["addr"]],
        )
        # Should fail — zone doesn't exist on server
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# federation status — two-node cluster (Leader + Follower profiles)
# ---------------------------------------------------------------------------


class TestStatusCluster:
    """Test 'federation status' with both Leader and Follower profiles."""

    def test_leader_status_json(self, two_node_cluster: dict) -> None:
        c = two_node_cluster
        runner = CliRunner()
        result = runner.invoke(
            federation,
            ["status", c["zone_id"], "--addr", c["leader_addr"], "--json"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["zone_id"] == c["zone_id"]
        assert data["is_leader"] is True
        assert data["role"] == "Leader"
        assert data["term"] > 0

    def test_follower_status_json(self, two_node_cluster: dict) -> None:
        c = two_node_cluster
        runner = CliRunner()
        result = runner.invoke(
            federation,
            ["status", c["zone_id"], "--addr", c["follower_addr"], "--json"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["zone_id"] == c["zone_id"]
        assert data["is_leader"] is False
        assert data["role"] == "Follower"
        assert data["leader_id"] > 0
        assert data["term"] > 0

    def test_leader_status_rich(self, two_node_cluster: dict) -> None:
        c = two_node_cluster
        runner = CliRunner()
        result = runner.invoke(
            federation,
            ["status", c["zone_id"], "--addr", c["leader_addr"]],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "Leader" in result.output
        assert c["zone_id"] in result.output

    def test_follower_status_rich(self, two_node_cluster: dict) -> None:
        c = two_node_cluster
        runner = CliRunner()
        result = runner.invoke(
            federation,
            ["status", c["zone_id"], "--addr", c["follower_addr"]],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "Follower" in result.output
        assert c["zone_id"] in result.output


# ---------------------------------------------------------------------------
# federation list — single node
# ---------------------------------------------------------------------------


class TestListSingleNode:
    """Test 'federation list' against real servers.

    Note: The list command creates its own ZoneManager (fresh in-memory
    registry) so it can only discover zones that it creates itself.
    For zone-discovery tests, we use the empty-directory path.
    The zone-query path is tested via status/discover commands.
    """

    def test_list_empty_data_dir_json(self) -> None:
        """An empty data directory should produce an empty JSON list."""
        empty_dir = tempfile.mkdtemp(prefix="nexus_e2e_empty_")
        list_port = _find_free_port()
        runner = CliRunner()
        result = runner.invoke(
            federation,
            [
                "list",
                "--data-dir",
                empty_dir,
                "--bind",
                f"127.0.0.1:{list_port}",
                "--node-id",
                "99",
                "--json",
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == []

    def test_list_empty_rich(self) -> None:
        """Empty data dir should show 'No zones found' in rich mode."""
        empty_dir = tempfile.mkdtemp(prefix="nexus_e2e_empty_")
        list_port = _find_free_port()
        runner = CliRunner()
        result = runner.invoke(
            federation,
            [
                "list",
                "--data-dir",
                empty_dir,
                "--bind",
                f"127.0.0.1:{list_port}",
                "--node-id",
                "99",
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "No zones found" in result.output


# ---------------------------------------------------------------------------
# federation discover — single node
# ---------------------------------------------------------------------------


class TestDiscoverSingleNode:
    """Test 'federation discover' against a real gRPC server."""

    def test_discover_rich_output(self, single_node_server: dict) -> None:
        srv = single_node_server
        runner = CliRunner()
        result = runner.invoke(
            federation,
            ["discover", srv["addr"]],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert srv["addr"] in result.output
        assert "OK" in result.output

    def test_discover_json_output(self, single_node_server: dict) -> None:
        srv = single_node_server
        runner = CliRunner()
        result = runner.invoke(
            federation,
            ["discover", srv["addr"], "--json"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["peer"] == srv["addr"]

        # Connection check — should succeed against real server
        assert data["checks"]["connection"]["status"] == "OK"

        # Cluster info — may fail because discover uses zone_id="" and
        # the server has no zone with an empty ID. That's expected behavior.
        assert data["checks"]["cluster_info"]["status"] in ("OK", "FAIL")

        # gRPC RTT — should have timing data
        rtt = data["checks"]["grpc_rtt_ms"]
        assert rtt["status"] in ("OK", "FAIL")

        # TLS — no TLS configured in test
        assert data["checks"]["tls"]["status"] == "N/A"
        assert data["checks"]["tls"]["mode"] == "insecure"

    def test_discover_dead_peer_json(self) -> None:
        """Discover a port with nothing listening — gRPC connect is lazy so
        connection appears OK, but cluster_info/rtt fail on actual RPC.
        """
        dead_port = _find_free_port()
        runner = CliRunner()
        result = runner.invoke(
            federation,
            ["discover", f"127.0.0.1:{dead_port}", "--json"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        # gRPC connect() is lazy — channel creation succeeds
        assert data["checks"]["connection"]["status"] == "OK"
        # Actual RPC calls fail against a dead port
        assert data["checks"]["cluster_info"]["status"] == "FAIL"
        assert data["checks"]["grpc_rtt_ms"]["status"] == "FAIL"

    def test_discover_dead_peer_rich(self) -> None:
        """Discover a dead port — rich output should show FAIL for RPCs."""
        dead_port = _find_free_port()
        runner = CliRunner()
        result = runner.invoke(
            federation,
            ["discover", f"127.0.0.1:{dead_port}"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "FAIL" in result.output


# ---------------------------------------------------------------------------
# federation discover — two-node cluster
# ---------------------------------------------------------------------------


class TestDiscoverCluster:
    """Test 'federation discover' against both nodes in a real cluster."""

    def test_discover_leader_json(self, two_node_cluster: dict) -> None:
        c = two_node_cluster
        runner = CliRunner()
        result = runner.invoke(
            federation,
            ["discover", c["leader_addr"], "--json"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["peer"] == c["leader_addr"]
        assert data["checks"]["connection"]["status"] == "OK"

    def test_discover_follower_json(self, two_node_cluster: dict) -> None:
        c = two_node_cluster
        runner = CliRunner()
        result = runner.invoke(
            federation,
            ["discover", c["follower_addr"], "--json"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["peer"] == c["follower_addr"]
        assert data["checks"]["connection"]["status"] == "OK"


# ---------------------------------------------------------------------------
# federation share — pull model (local only, no peer spec)
# ---------------------------------------------------------------------------


class TestShareReal:
    """Test 'federation share' against real servers — no mocking.

    Pull model: share is purely local (creates zone + DT_MOUNT).
    The share command creates its own ZoneManager; without a bootstrapped
    root zone, share_subtree() fails — we verify graceful error handling.
    """

    def test_share_no_root_zone(self, single_node_server: dict) -> None:
        """Share with valid path but no root zone → graceful error.

        Exercises the real share code path: ZoneManager created,
        but share_subtree fails because there's no root zone
        (the CLI's internal ZoneManager is brand new).
        """
        share_port = _find_free_port()
        runner = CliRunner()
        result = runner.invoke(
            federation,
            [
                "share",
                "/my/project",
                "--data-dir",
                tempfile.mkdtemp(prefix="nexus_e2e_share_"),
                "--bind",
                f"127.0.0.1:{share_port}",
                "--node-id",
                "99",
            ],
        )
        # Should fail gracefully — root zone not found
        assert result.exit_code != 0

    def test_share_no_root_zone_json(self, single_node_server: dict) -> None:
        """Share failure in JSON mode should still exit non-zero."""
        share_port = _find_free_port()
        runner = CliRunner()
        result = runner.invoke(
            federation,
            [
                "share",
                "/my/project",
                "--json",
                "--data-dir",
                tempfile.mkdtemp(prefix="nexus_e2e_share_"),
                "--bind",
                f"127.0.0.1:{share_port}",
                "--node-id",
                "99",
            ],
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# federation join — real server tests, no mocking
# ---------------------------------------------------------------------------


class TestJoinReal:
    """Test 'federation join' against real servers — no mocking.

    Tests peer spec validation AND real federation code paths.
    """

    def test_join_bad_peer_spec(self) -> None:
        """Bad peer spec should fail (CLI-level validation)."""
        runner = CliRunner()
        result = runner.invoke(
            federation,
            ["join", "no-path-spec", "/local"],
        )
        assert result.exit_code != 0
        assert "Invalid peer spec" in result.output

    def test_join_root_path_rejected(self) -> None:
        """Root path '/' should be rejected (CLI-level validation)."""
        runner = CliRunner()
        result = runner.invoke(
            federation,
            ["join", "host:2126:/", "/local"],
        )
        assert result.exit_code != 0

    def test_join_real_peer_no_mount(self, single_node_server: dict) -> None:
        """Join with valid peer spec pointing at real server — graceful error.

        Exercises real code: peer spec parsed, ZoneManager created,
        NexusFederation.join() attempts to discover DT_MOUNT on peer.
        Fails because the remote path isn't a DT_MOUNT (VFS service not running).
        """
        srv = single_node_server
        join_port = _find_free_port()
        runner = CliRunner()
        result = runner.invoke(
            federation,
            [
                "join",
                f"{srv['addr']}:/nonexistent/path",
                "/local/mount",
                "--data-dir",
                tempfile.mkdtemp(prefix="nexus_e2e_join_"),
                "--bind",
                f"127.0.0.1:{join_port}",
                "--node-id",
                "99",
            ],
        )
        # Should fail — VFS service not running or path doesn't exist as DT_MOUNT
        assert result.exit_code != 0

    def test_join_dead_peer(self) -> None:
        """Join a non-existent peer — should timeout or fail gracefully."""
        dead_port = _find_free_port()
        join_port = _find_free_port()
        runner = CliRunner()
        result = runner.invoke(
            federation,
            [
                "join",
                f"127.0.0.1:{dead_port}:/remote/path",
                "/local/mount",
                "--timeout",
                "3",
                "--data-dir",
                tempfile.mkdtemp(prefix="nexus_e2e_join_"),
                "--bind",
                f"127.0.0.1:{join_port}",
                "--node-id",
                "99",
            ],
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Peer spec parsing (real function, no mocking)
# ---------------------------------------------------------------------------


class TestParsePeerSpec:
    """Test _parse_peer_spec with real function — no mocking."""

    def test_valid_spec(self) -> None:
        addr, path = _parse_peer_spec("peer1:2126:/shared/data")
        assert addr == "peer1:2126"
        assert path == "/shared/data"

    def test_valid_spec_with_ip(self) -> None:
        addr, path = _parse_peer_spec("10.0.0.1:2126:/path")
        assert addr == "10.0.0.1:2126"
        assert path == "/path"

    def test_valid_spec_deep_path(self) -> None:
        addr, path = _parse_peer_spec("host:2126:/a/b/c/d")
        assert addr == "host:2126"
        assert path == "/a/b/c/d"

    def test_missing_path(self) -> None:
        with pytest.raises(Exception, match="Invalid peer spec"):
            _parse_peer_spec("host:2126")

    def test_empty_address(self) -> None:
        with pytest.raises(Exception, match="Empty address"):
            _parse_peer_spec(":/path")

    def test_root_path_rejected(self) -> None:
        with pytest.raises(Exception, match="Empty or root path"):
            _parse_peer_spec("host:2126:/")


# ---------------------------------------------------------------------------
# Cross-cutting: timeout behavior
# ---------------------------------------------------------------------------


class TestTimeout:
    """Test that --timeout flag works against real server."""

    def test_status_with_short_timeout(self, single_node_server: dict) -> None:
        """Even a short timeout should succeed for a local server."""
        srv = single_node_server
        runner = CliRunner()
        result = runner.invoke(
            federation,
            ["status", srv["zone_id"], "--addr", srv["addr"], "--timeout", "5", "--json"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["zone_id"] == srv["zone_id"]

    def test_discover_with_timeout(self, single_node_server: dict) -> None:
        srv = single_node_server
        runner = CliRunner()
        result = runner.invoke(
            federation,
            ["discover", srv["addr"], "--timeout", "5", "--json"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["checks"]["connection"]["status"] == "OK"


# ---------------------------------------------------------------------------
# Multi-zone scenario
# ---------------------------------------------------------------------------


class TestMultiZone:
    """Test with multiple zones on a single server."""

    @pytest.fixture(scope="class")
    def multi_zone_server(self):
        """Server with 2 zones bootstrapped."""
        tmpdir = tempfile.mkdtemp(prefix="nexus_e2e_multi_")
        port = _find_free_port()
        bind_addr = f"127.0.0.1:{port}"

        mgr = _create_zone_manager(node_id=1, base_path=tmpdir, bind_addr=bind_addr)
        mgr.create_zone("zone-alpha", [])
        mgr.create_zone("zone-beta", [])

        # Wait for both zones to elect leaders
        _wait_for_leader(bind_addr, "zone-alpha")
        _wait_for_leader(bind_addr, "zone-beta")

        yield {
            "mgr": mgr,
            "addr": bind_addr,
            "data_dir": tmpdir,
        }

        mgr.shutdown()

    def test_status_each_zone_json(self, multi_zone_server: dict) -> None:
        srv = multi_zone_server
        runner = CliRunner()
        for zone_id in ("zone-alpha", "zone-beta"):
            result = runner.invoke(
                federation,
                ["status", zone_id, "--addr", srv["addr"], "--json"],
                catch_exceptions=False,
            )
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["zone_id"] == zone_id
            assert data["is_leader"] is True

    def test_status_each_zone_rich(self, multi_zone_server: dict) -> None:
        srv = multi_zone_server
        runner = CliRunner()
        for zone_id in ("zone-alpha", "zone-beta"):
            result = runner.invoke(
                federation,
                ["status", zone_id, "--addr", srv["addr"]],
                catch_exceptions=False,
            )
            assert result.exit_code == 0
            assert zone_id in result.output
            assert "Leader" in result.output

    def test_discover_multi_zone_server(self, multi_zone_server: dict) -> None:
        srv = multi_zone_server
        runner = CliRunner()
        result = runner.invoke(
            federation,
            ["discover", srv["addr"], "--json"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["checks"]["connection"]["status"] == "OK"


# ---------------------------------------------------------------------------
# federation share — full success path (pull model: local zone creation)
# ---------------------------------------------------------------------------


def _wait_for_leader_and_topology(mgr: Any, addr: str, zone_id: str) -> None:
    """Wait for leader election then ensure root '/' exists."""
    _wait_for_leader(addr, zone_id, timeout=15.0)
    # ensure_topology creates "/" in root zone — retry a few times
    for _ in range(20):
        if mgr.ensure_topology():
            return
        time.sleep(0.3)
    raise TimeoutError(f"ensure_topology() didn't converge for zone '{zone_id}'")


class TestShareSuccessPath:
    """Full share success path (pull model): zone creation + metadata copy.

    Exercises the REAL end-to-end flow:
        1. Node 1 bootstraps root zone + creates test data at /projects
        2. Node 1 calls fed.share("/projects") — purely local operation
        3. Verify: new zone created, DT_MOUNT at /projects, metadata rebased

    No mocking. Real gRPC server, real Raft consensus.
    """

    @pytest.fixture(scope="class")
    def shared_zone(self):
        """Single ZoneManager with bootstrapped root zone and shared subtree."""
        from nexus.contracts.metadata import DT_DIR, DT_REG, FileMetadata
        from nexus.raft.zone_manager import ZoneManager

        # Disable auto-TLS so node uses plain gRPC
        with patch.object(ZoneManager, "_auto_generate_tls", staticmethod(lambda *_a, **_kw: None)):
            tmpdir = tempfile.mkdtemp(prefix="nexus_e2e_share_n1_")
            port = _find_free_port()
            addr = f"127.0.0.1:{port}"

            mgr = ZoneManager(node_id=1, base_path=tmpdir, bind_addr=addr)

            # Bootstrap root zone (single-node, self-elects as leader)
            mgr.bootstrap()
            _wait_for_leader_and_topology(mgr, addr, "root")

            # Seed test data: /projects directory with files
            root_store = mgr.get_store("root")
            assert root_store is not None

            root_store.put(
                FileMetadata(
                    path="/projects",
                    backend_name="virtual",
                    physical_path="",
                    size=0,
                    entry_type=DT_DIR,
                    zone_id="root",
                )
            )
            root_store.put(
                FileMetadata(
                    path="/projects/readme.txt",
                    backend_name="local",
                    physical_path="/data/readme.txt",
                    size=1024,
                    entry_type=DT_REG,
                    zone_id="root",
                )
            )
            root_store.put(
                FileMetadata(
                    path="/projects/src",
                    backend_name="virtual",
                    physical_path="",
                    size=0,
                    entry_type=DT_DIR,
                    zone_id="root",
                )
            )

            # Execute the share: purely local, creates zone + DT_MOUNT
            from nexus.raft.federation import NexusFederation

            fed = NexusFederation(zone_manager=mgr)
            zone_id = asyncio.run(
                asyncio.wait_for(
                    fed.share("/projects"),
                    timeout=30.0,
                )
            )

            yield {
                "mgr": mgr,
                "addr": addr,
                "root_store": root_store,
                "zone_id": zone_id,
            }

            mgr.shutdown()

    def test_share_creates_zone_and_mounts(self, shared_zone: dict) -> None:
        """Share creates new zone + DT_MOUNT at /projects."""
        mgr = shared_zone["mgr"]
        zone_id = shared_zone["zone_id"]

        # New zone was created
        assert zone_id is not None
        assert len(zone_id) > 0

        # New zone exists on Node
        new_store = mgr.get_store(zone_id)
        assert new_store is not None, f"Zone '{zone_id}' not found"

        # DT_MOUNT at /projects in root zone
        root = shared_zone["root_store"]
        mount_entry = root.get("/projects")
        assert mount_entry is not None, "DT_MOUNT not found at /projects"
        assert mount_entry.is_mount, f"Expected DT_MOUNT, got entry_type={mount_entry.entry_type}"
        assert mount_entry.target_zone_id == zone_id

    def test_share_metadata_rebased(self, shared_zone: dict) -> None:
        """Metadata is copied into new zone with rebased paths."""
        mgr = shared_zone["mgr"]
        zone_id = shared_zone["zone_id"]
        new_store = mgr.get_store(zone_id)
        assert new_store is not None

        # Root '/' of new zone
        root_dir = new_store.get("/")
        assert root_dir is not None, "Root '/' not found in new zone"
        assert root_dir.is_dir

        # /readme.txt (rebased from /projects/readme.txt)
        readme = new_store.get("/readme.txt")
        assert readme is not None, "/readme.txt not found in new zone"
        assert readme.size == 1024
        assert readme.backend_name == "local"

        # /src (rebased from /projects/src)
        src_dir = new_store.get("/src")
        assert src_dir is not None, "/src not found in new zone"
        assert src_dir.is_dir

    def test_share_zone_is_leader(self, shared_zone: dict) -> None:
        """Shared zone should have this node as leader (single-node cluster)."""
        addr = shared_zone["addr"]
        zone_id = shared_zone["zone_id"]
        info = _get_cluster_info(addr, zone_id)
        assert info["is_leader"] is True

    def test_share_cli_status_shows_zone(self, shared_zone: dict) -> None:
        """CLI 'federation status' should work against the shared zone."""
        addr = shared_zone["addr"]
        zone_id = shared_zone["zone_id"]
        runner = CliRunner()
        result = runner.invoke(
            federation,
            ["status", zone_id, "--addr", addr, "--json"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["zone_id"] == zone_id
        assert data["is_leader"] is True


# ---------------------------------------------------------------------------
# federation join — full success path (real cross-node join)
# ---------------------------------------------------------------------------


class TestJoinSuccessPath:
    """Full join success path: join zone via ZoneManager + JoinZone RPC.

    Two-node scenario:
        - Node 1 shares /projects (local, pull model)
        - Node 2 joins the zone via ZoneManager.join_zone + JoinZone RPC
        - Node 2 mounts the zone at /local_mount

    Uses ZoneManager.join_zone + JoinZone RPC directly (bypasses VFS discovery
    since NexusVFSService is not available in PyO3-only tests).
    """

    @pytest.fixture(scope="class")
    def two_node_federation(self):
        """Two nodes: Node 1 shares, Node 2 joins via Raft protocol."""
        from nexus.contracts.metadata import DT_DIR, DT_REG, FileMetadata
        from nexus.raft.federation import NexusFederation
        from nexus.raft.zone_manager import ZoneManager

        with patch.object(ZoneManager, "_auto_generate_tls", staticmethod(lambda *_a, **_kw: None)):
            tmpdir1 = tempfile.mkdtemp(prefix="nexus_e2e_join_n1_")
            tmpdir2 = tempfile.mkdtemp(prefix="nexus_e2e_join_n2_")
            port1 = _find_free_port()
            port2 = _find_free_port()
            addr1 = f"127.0.0.1:{port1}"
            addr2 = f"127.0.0.1:{port2}"

            mgr1 = ZoneManager(node_id=1, base_path=tmpdir1, bind_addr=addr1)
            mgr2 = ZoneManager(node_id=2, base_path=tmpdir2, bind_addr=addr2)

            # Bootstrap root zones
            mgr1.bootstrap()
            mgr2.bootstrap()

            _wait_for_leader_and_topology(mgr1, addr1, "root")
            _wait_for_leader_and_topology(mgr2, addr2, "root")

            # Seed data on Node 1
            root1 = mgr1.get_store("root")
            assert root1 is not None
            root1.put(
                FileMetadata(
                    path="/projects",
                    backend_name="virtual",
                    physical_path="",
                    size=0,
                    entry_type=DT_DIR,
                    zone_id="root",
                )
            )
            root1.put(
                FileMetadata(
                    path="/projects/readme.txt",
                    backend_name="local",
                    physical_path="/data/readme.txt",
                    size=2048,
                    entry_type=DT_REG,
                    zone_id="root",
                )
            )

            # Node 1 shares /projects (pull model — purely local)
            fed1 = NexusFederation(zone_manager=mgr1)
            zone_id = asyncio.run(asyncio.wait_for(fed1.share("/projects"), timeout=30.0))

            # Node 2 joins the shared zone using Raft protocol directly
            # (bypasses VFS discovery which needs NexusVFSService)
            mgr2.join_zone(zone_id, peers=[addr1])

            # Request membership via JoinZone RPC
            from grpc import aio as grpc_aio

            from nexus.raft import transport_pb2, transport_pb2_grpc

            async def _join_rpc():
                channel = grpc_aio.insecure_channel(addr1)
                try:
                    stub = transport_pb2_grpc.ZoneApiServiceStub(channel)
                    resp = await stub.JoinZone(
                        transport_pb2.JoinZoneRequest(
                            zone_id=zone_id,
                            node_id=2,
                            node_address=addr2,
                        ),
                        timeout=15.0,
                    )
                    assert resp.success, f"JoinZone failed: {resp.error}"
                finally:
                    await channel.close()

            asyncio.run(asyncio.wait_for(_join_rpc(), timeout=20.0))

            # Wait for Node 2 to have the zone
            _wait_for_leader(addr2, zone_id, timeout=15.0)

            # Mount zone in Node 2's root zone
            mgr2.mount("root", "/local_mount", zone_id)

            yield {
                "mgr1": mgr1,
                "mgr2": mgr2,
                "addr1": addr1,
                "addr2": addr2,
                "root_store1": root1,
                "zone_id": zone_id,
            }

            mgr1.shutdown()
            mgr2.shutdown()

    def test_join_zone_exists_on_both_nodes(self, two_node_federation: dict) -> None:
        """After join, the shared zone exists on both nodes."""
        data = two_node_federation
        zones1 = data["mgr1"].list_zones()
        zones2 = data["mgr2"].list_zones()
        assert data["zone_id"] in zones1
        assert data["zone_id"] in zones2

    def test_join_node2_has_mount(self, two_node_federation: dict) -> None:
        """Node 2 should have a DT_MOUNT at /local_mount."""
        data = two_node_federation
        root2 = data["mgr2"].get_store("root")
        assert root2 is not None
        mount_entry = root2.get("/local_mount")
        assert mount_entry is not None, "DT_MOUNT not found at /local_mount on Node 2"
        assert mount_entry.is_mount
        assert mount_entry.target_zone_id == data["zone_id"]

    def test_join_data_replicated(self, two_node_federation: dict) -> None:
        """Raft log replication: shared zone metadata readable on Node 2."""
        data = two_node_federation
        zone_id = data["zone_id"]
        store2 = data["mgr2"].get_store(zone_id)

        # Allow time for replication
        readme = None
        for _ in range(30):
            readme = store2.get("/readme.txt") if store2 else None
            if readme is not None:
                break
            time.sleep(0.5)

        assert readme is not None, "/readme.txt not replicated to Node 2"
        assert readme.size == 2048
        assert readme.backend_name == "local"

    def test_join_cluster_info_shows_zone(self, two_node_federation: dict) -> None:
        """CLI 'federation status' should show the zone on both nodes."""
        data = two_node_federation
        zone_id = data["zone_id"]
        runner = CliRunner()

        for addr in (data["addr1"], data["addr2"]):
            result = runner.invoke(
                federation,
                ["status", zone_id, "--addr", addr, "--json"],
                catch_exceptions=False,
            )
            assert result.exit_code == 0
            info = json.loads(result.output)
            assert info["zone_id"] == zone_id
            assert info["term"] > 0
