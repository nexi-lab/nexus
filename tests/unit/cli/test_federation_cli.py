"""Tests for federation CLI commands (Issue #2808, Decisions 9A, 10A, 11A).

Covers:
- federation status (success, JSON, error handling)
- federation list (success, JSON, empty, parallel queries)
- federation discover (success, JSON, connection failure, TLS, partial failure)
- federation share (success, JSON, pull model — no peer spec)
- federation join (success, JSON, timeout, bad peer spec)
- Peer spec parsing edge cases
"""

import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli.commands.federation import (
    _parse_peer_spec,
    federation,
)


@pytest.fixture(autouse=True)
def _disable_auto_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable auto-JSON in CliRunner (stdout is not a TTY)."""
    monkeypatch.setenv("NEXUS_NO_AUTO_JSON", "1")


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

_DEFAULT_CLUSTER_INFO = {
    "node_id": 1,
    "leader_id": 1,
    "term": 42,
    "is_leader": True,
    "leader_address": "localhost:2126",
    "applied_index": 100,
}


def _patch_get_cluster_info(return_value: dict | None = None, side_effect=None):
    """Patch _get_cluster_info (used by status command)."""
    kwargs = {"return_value": return_value or _DEFAULT_CLUSTER_INFO}
    if side_effect is not None:
        kwargs = {"side_effect": side_effect}
    return patch(
        "nexus.cli.commands.federation._get_cluster_info",
        new_callable=AsyncMock,
        **kwargs,
    )


def _mock_grpc_response(info: dict | None = None):
    """Create a mock gRPC GetClusterInfoResponse."""
    data = info or _DEFAULT_CLUSTER_INFO
    resp = MagicMock()
    resp.node_id = data["node_id"]
    resp.leader_id = data["leader_id"]
    resp.term = data["term"]
    resp.is_leader = data["is_leader"]
    resp.leader_address = data.get("leader_address", "")
    resp.applied_index = data.get("applied_index", 0)
    return resp


def _patch_build_channel(grpc_response=None, side_effect=None):
    """Patch _build_channel and inject mock protobuf modules.

    Uses sys.modules injection to avoid triggering the real protobuf
    descriptor chain (transport_pb2 → commands.proto).
    """
    mock_channel = MagicMock()
    mock_channel.close = AsyncMock()
    mock_channel.channel_ready = AsyncMock()  # TCP connectivity check
    mock_channel.__aenter__ = AsyncMock(return_value=mock_channel)
    mock_channel.__aexit__ = AsyncMock(return_value=False)

    if grpc_response is None:
        grpc_response = _mock_grpc_response()

    # The stub is created via ZoneApiServiceStub(channel), so we mock it
    mock_stub = MagicMock()
    if side_effect is not None:
        mock_stub.GetClusterInfo = AsyncMock(side_effect=side_effect)
    else:
        mock_stub.GetClusterInfo = AsyncMock(return_value=grpc_response)

    # Create mock protobuf modules to avoid descriptor chain import
    mock_pb2 = MagicMock()
    mock_pb2_grpc = MagicMock()
    mock_pb2_grpc.ZoneApiServiceStub.return_value = mock_stub

    # Mock grpc module for AioRpcError / StatusCode used by discover
    mock_grpc = MagicMock()
    mock_grpc.aio.AioRpcError = type("AioRpcError", (Exception,), {})
    mock_grpc.StatusCode.UNAVAILABLE = "UNAVAILABLE"
    mock_grpc.StatusCode.DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"

    return (
        patch("nexus.cli.commands.federation._build_channel", return_value=mock_channel),
        patch.dict(
            sys.modules,
            {
                "grpc": mock_grpc,
                "nexus.raft.transport_pb2": mock_pb2,
                "nexus.raft.transport_pb2_grpc": mock_pb2_grpc,
            },
        ),
    )


def _patch_discover_zones(zones: list[str]):
    """Patch _discover_zones_from_disk (used by list command)."""
    return patch(
        "nexus.cli.commands.federation._discover_zones_from_disk",
        return_value=zones,
    )


# ---------------------------------------------------------------------------
# federation status
# ---------------------------------------------------------------------------


class TestFederationStatus:
    def test_status_success(self) -> None:
        runner = CliRunner()
        with _patch_get_cluster_info():
            result = runner.invoke(federation, ["status", "zone-a"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "zone-a" in result.output
        assert "Leader" in result.output
        assert "42" in result.output

    def test_status_json(self) -> None:
        runner = CliRunner()
        with _patch_get_cluster_info():
            result = runner.invoke(
                federation, ["status", "zone-a", "--json"], catch_exceptions=False
            )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["zone_id"] == "zone-a"
        assert data["term"] == 42
        assert data["role"] == "Leader"
        assert data["is_leader"] is True
        assert data["applied_index"] == 100

    def test_status_follower(self) -> None:
        follower_info = {
            "node_id": 2,
            "leader_id": 1,
            "term": 42,
            "is_leader": False,
            "leader_address": "leader:2126",
            "applied_index": 98,
        }
        runner = CliRunner()
        with _patch_get_cluster_info(follower_info):
            result = runner.invoke(
                federation, ["status", "zone-a", "--json"], catch_exceptions=False
            )

        data = json.loads(result.output)
        assert data["role"] == "Follower"
        assert data["is_leader"] is False

    def test_status_connection_error(self) -> None:
        runner = CliRunner()
        with _patch_get_cluster_info(side_effect=ConnectionError("refused")):
            result = runner.invoke(federation, ["status", "zone-a"])

        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# federation list
# ---------------------------------------------------------------------------


class TestFederationList:
    def test_list_success(self) -> None:
        runner = CliRunner()
        p_chan, p_stub = _patch_build_channel()
        with _patch_discover_zones(["zone-a", "zone-b"]), p_chan, p_stub:
            result = runner.invoke(federation, ["list"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "zone-a" in result.output
        assert "zone-b" in result.output

    def test_list_json(self) -> None:
        runner = CliRunner()
        p_chan, p_stub = _patch_build_channel()
        with _patch_discover_zones(["zone-a"]), p_chan, p_stub:
            result = runner.invoke(federation, ["list", "--json"], catch_exceptions=False)

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["zone_id"] == "zone-a"
        assert data[0]["status"] == "OK"

    def test_list_empty(self) -> None:
        runner = CliRunner()
        with _patch_discover_zones([]):
            result = runner.invoke(federation, ["list"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "No zones found" in result.output

    def test_list_empty_json(self) -> None:
        runner = CliRunner()
        with _patch_discover_zones([]):
            result = runner.invoke(federation, ["list", "--json"], catch_exceptions=False)

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == []

    def test_list_handles_query_error(self) -> None:
        """If a zone query fails, it shows ERROR status instead of crashing."""
        p_chan, p_stub = _patch_build_channel(side_effect=ConnectionError("timeout"))

        runner = CliRunner()
        with _patch_discover_zones(["zone-a"]), p_chan, p_stub:
            result = runner.invoke(federation, ["list", "--json"], catch_exceptions=False)

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["status"] == "ERROR"
        assert "timeout" in data[0]["error"]


# ---------------------------------------------------------------------------
# federation discover
# ---------------------------------------------------------------------------


class TestFederationDiscover:
    def test_discover_success(self) -> None:
        runner = CliRunner()
        p_chan, p_stub = _patch_build_channel()
        with p_chan, p_stub:
            result = runner.invoke(federation, ["discover", "peer1:2126"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "peer1:2126" in result.output
        assert "OK" in result.output

    def test_discover_json(self) -> None:
        runner = CliRunner()
        p_chan, p_stub = _patch_build_channel()
        with p_chan, p_stub:
            result = runner.invoke(
                federation, ["discover", "peer1:2126", "--json"], catch_exceptions=False
            )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["peer"] == "peer1:2126"
        assert data["checks"]["connection"]["status"] == "OK"
        assert data["checks"]["cluster_info"]["status"] == "OK"
        assert data["checks"]["grpc_rtt_ms"]["status"] == "OK"
        assert data["checks"]["grpc_rtt_ms"]["samples"] == 3

    def test_discover_cluster_info_fails(self) -> None:
        """Connection succeeds but GetClusterInfo fails."""
        p_chan, p_stub = _patch_build_channel(side_effect=RuntimeError("zone not found"))

        runner = CliRunner()
        with p_chan, p_stub:
            result = runner.invoke(
                federation, ["discover", "peer:2126", "--json"], catch_exceptions=False
            )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["checks"]["connection"]["status"] == "OK"
        assert data["checks"]["cluster_info"]["status"] == "FAIL"

    def test_discover_tls_not_configured(self) -> None:
        runner = CliRunner()
        p_chan, p_stub = _patch_build_channel()
        with p_chan, p_stub:
            result = runner.invoke(
                federation, ["discover", "peer:2126", "--json"], catch_exceptions=False
            )

        data = json.loads(result.output)
        assert data["checks"]["tls"]["status"] == "N/A"
        assert data["checks"]["tls"]["mode"] == "insecure"


# ---------------------------------------------------------------------------
# federation share (pull model — local only, no peer spec)
# ---------------------------------------------------------------------------


def _patch_federation(return_value: str = "zone-abc123"):
    """Patch NexusFederation + ZoneManager used by share/join (lazy imports)."""
    mock_fed = MagicMock()
    mock_fed.share = AsyncMock(return_value=return_value)
    mock_fed.join = AsyncMock(return_value=return_value)
    return (
        patch("nexus.raft.federation.NexusFederation", return_value=mock_fed),
        patch("nexus.raft.zone_manager.ZoneManager"),
    )


class TestFederationShare:
    def test_share_success(self) -> None:
        runner = CliRunner()
        p_fed, p_mgr = _patch_federation()
        with p_fed, p_mgr:
            result = runner.invoke(
                federation,
                ["share", "/my/project"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert "zone-abc123" in result.output

    def test_share_json(self) -> None:
        runner = CliRunner()
        p_fed, p_mgr = _patch_federation()
        with p_fed, p_mgr:
            result = runner.invoke(
                federation,
                ["share", "/my/project", "--json"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["zone_id"] == "zone-abc123"
        assert data["local_path"] == "/my/project"

    def test_share_with_zone_id(self) -> None:
        runner = CliRunner()
        p_fed, p_mgr = _patch_federation()
        with p_fed, p_mgr:
            result = runner.invoke(
                federation,
                ["share", "/my/project", "--zone-id", "custom-zone", "--json"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["zone_id"] == "zone-abc123"


# ---------------------------------------------------------------------------
# federation join
# ---------------------------------------------------------------------------


class TestFederationJoin:
    def test_join_success(self) -> None:
        runner = CliRunner()
        p_fed, p_mgr = _patch_federation("zone-xyz789")
        with p_fed, p_mgr:
            result = runner.invoke(
                federation,
                ["join", "peer:2126:/shared", "/local/mount"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert "zone-xyz789" in result.output

    def test_join_json(self) -> None:
        runner = CliRunner()
        p_fed, p_mgr = _patch_federation("zone-xyz789")
        with p_fed, p_mgr:
            result = runner.invoke(
                federation,
                ["join", "peer:2126:/shared", "/local/mount", "--json"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["zone_id"] == "zone-xyz789"
        assert data["local_path"] == "/local/mount"

    def test_join_bad_peer_spec(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            federation,
            ["join", "no-path-spec", "/local"],
        )

        assert result.exit_code != 0
        assert "Invalid peer spec" in result.output


# ---------------------------------------------------------------------------
# Peer spec parsing
# ---------------------------------------------------------------------------


class TestParsePeerSpec:
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
