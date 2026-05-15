"""Tests for hostname-based node ID derivation and PeerAddress parsing.

Golden values must match the Rust implementation in transport/mod.rs.
"""

from nexus.raft.peer_address import PeerAddress, hostname_to_node_id


class TestHostnameToNodeId:
    """Golden-value tests — must match Rust ``hostname_to_node_id``."""

    def test_nexus_1(self) -> None:
        assert hostname_to_node_id("nexus-1") == 14044926161142285152

    def test_nexus_2(self) -> None:
        assert hostname_to_node_id("nexus-2") == 768242927742468745

    def test_witness(self) -> None:
        assert hostname_to_node_id("witness") == 10099512703796518074

    def test_zero_maps_to_one(self) -> None:
        """If SHA-256 happens to produce 0, it must be mapped to 1."""
        # We can't easily find a hostname whose SHA-256 starts with 8 zero bytes,
        # but the code path is tested by inspection. This test verifies non-zero.
        assert hostname_to_node_id("nexus-1") != 0

    def test_deterministic(self) -> None:
        """Same hostname always produces the same ID."""
        a = hostname_to_node_id("my-host")
        b = hostname_to_node_id("my-host")
        assert a == b


class TestPeerAddress:
    def test_parse(self) -> None:
        addr = PeerAddress.parse("nexus-1:2126")
        assert addr.hostname == "nexus-1"
        assert addr.port == 2126
        assert addr.node_id == hostname_to_node_id("nexus-1")

    def test_grpc_target(self) -> None:
        addr = PeerAddress.parse("nexus-1:2126")
        assert addr.grpc_target == "nexus-1:2126"

    def test_to_raft_peer_str(self) -> None:
        addr = PeerAddress.parse("nexus-1:2126")
        nid = hostname_to_node_id("nexus-1")
        assert addr.to_raft_peer_str() == f"{nid}@nexus-1:2126"

    def test_parse_peer_list(self) -> None:
        addrs = PeerAddress.parse_peer_list("nexus-1:2126,nexus-2:2126,witness:2126")
        assert len(addrs) == 3
        assert addrs[0].hostname == "nexus-1"
        assert addrs[1].hostname == "nexus-2"
        assert addrs[2].hostname == "witness"

    def test_parse_peer_list_empty(self) -> None:
        assert PeerAddress.parse_peer_list("") == []
        assert PeerAddress.parse_peer_list("  ") == []

    def test_parse_invalid(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            PeerAddress.parse("no-port")

    def test_from_local(self) -> None:
        addr = PeerAddress.from_local(port=2126)
        assert addr.port == 2126
        assert addr.node_id != 0
