"""Tests for WireGuard network module — Issue #2960 (network zero tests).

Covers the security-critical paths: identity generation, key file permissions,
peer management, and config generation.
"""

import json
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.network.constants import WG_DEFAULT_PORT, WG_SUBNET
from nexus.network.wireguard import (
    add_peer,
    generate_wg_config,
    get_node_ip,
    load_identity,
    load_peers,
    remove_peer,
)


class TestGetNodeIP:
    """Test IP address assignment."""

    def test_valid_node_id(self) -> None:
        assert get_node_ip(1) == f"{WG_SUBNET}.1"
        assert get_node_ip(254) == f"{WG_SUBNET}.254"

    def test_boundary_values(self) -> None:
        assert get_node_ip(1) == f"{WG_SUBNET}.1"
        assert get_node_ip(254) == f"{WG_SUBNET}.254"

    def test_invalid_node_id_zero(self) -> None:
        with pytest.raises(ValueError, match="node_id must be 1-254"):
            get_node_ip(0)

    def test_invalid_node_id_too_high(self) -> None:
        with pytest.raises(ValueError, match="node_id must be 1-254"):
            get_node_ip(255)

    def test_negative_node_id(self) -> None:
        with pytest.raises(ValueError):
            get_node_ip(-1)


class TestInitIdentity:
    """Test identity initialization and secure storage."""

    @patch("nexus.network.wireguard.generate_keypair")
    def test_identity_file_has_restricted_permissions(
        self, mock_keygen: MagicMock, tmp_path: Path
    ) -> None:
        """Regression: C4 — identity file must be 0o600, not world-readable."""
        mock_keygen.return_value = ("fake_privkey", "fake_pubkey")

        with patch("nexus.network.wireguard.NETWORK_DIR", tmp_path / "network"), patch(
            "nexus.network.wireguard.PEERS_DIR", tmp_path / "network" / "peers"
        ):
            from nexus.network.wireguard import init_identity

            identity = init_identity(node_id=1)

        identity_path = tmp_path / "network" / "identity.json"
        assert identity_path.exists()

        mode = stat.S_IMODE(identity_path.stat().st_mode)
        assert mode == 0o600, f"Identity file should be 0o600, got {oct(mode)}"

        # Verify content
        data = json.loads(identity_path.read_text())
        assert data["private_key"] == "fake_privkey"
        assert data["public_key"] == "fake_pubkey"
        assert data["node_id"] == 1

    @patch("nexus.network.wireguard.generate_keypair")
    def test_identity_contains_all_fields(
        self, mock_keygen: MagicMock, tmp_path: Path
    ) -> None:
        mock_keygen.return_value = ("priv", "pub")

        with patch("nexus.network.wireguard.NETWORK_DIR", tmp_path / "network"), patch(
            "nexus.network.wireguard.PEERS_DIR", tmp_path / "network" / "peers"
        ):
            from nexus.network.wireguard import init_identity

            identity = init_identity(node_id=5, listen_port=12345)

        assert identity["node_id"] == 5
        assert identity["private_key"] == "priv"
        assert identity["public_key"] == "pub"
        assert identity["listen_port"] == 12345
        assert identity["ip"] == f"{WG_SUBNET}.5"


class TestLoadIdentity:
    """Test identity loading."""

    def test_load_missing_identity(self, tmp_path: Path) -> None:
        with patch("nexus.network.wireguard.NETWORK_DIR", tmp_path / "network"):
            with pytest.raises(FileNotFoundError):
                load_identity()


class TestPeerManagement:
    """Test peer add/load/remove operations."""

    def test_add_and_load_peer(self, tmp_path: Path) -> None:
        peers_dir = tmp_path / "peers"
        with patch("nexus.network.wireguard.PEERS_DIR", peers_dir):
            peer = add_peer(node_id=2, public_key="pk_peer2", endpoint="1.2.3.4:51820")

            assert peer["node_id"] == 2
            assert peer["public_key"] == "pk_peer2"
            assert peer["endpoint"] == "1.2.3.4:51820"
            assert peer["ip"] == f"{WG_SUBNET}.2"

            # Verify file written
            peer_path = peers_dir / "2.json"
            assert peer_path.exists()
            data = json.loads(peer_path.read_text())
            assert data == peer

    def test_load_peers_empty(self, tmp_path: Path) -> None:
        with patch("nexus.network.wireguard.PEERS_DIR", tmp_path / "nonexistent"):
            assert load_peers() == []

    def test_load_multiple_peers(self, tmp_path: Path) -> None:
        peers_dir = tmp_path / "peers"
        with patch("nexus.network.wireguard.PEERS_DIR", peers_dir):
            add_peer(1, "pk1", "1.1.1.1:51820")
            add_peer(3, "pk3", "3.3.3.3:51820")
            add_peer(2, "pk2", "2.2.2.2:51820")

            peers = load_peers()
            assert len(peers) == 3
            # Should be sorted by filename (node_id)
            assert peers[0]["node_id"] == 1
            assert peers[1]["node_id"] == 2
            assert peers[2]["node_id"] == 3

    def test_remove_peer(self, tmp_path: Path) -> None:
        peers_dir = tmp_path / "peers"
        with patch("nexus.network.wireguard.PEERS_DIR", peers_dir):
            add_peer(5, "pk5", "5.5.5.5:51820")
            assert remove_peer(5) is True
            assert remove_peer(5) is False  # Already removed

    def test_remove_nonexistent_peer(self, tmp_path: Path) -> None:
        peers_dir = tmp_path / "peers"
        peers_dir.mkdir(parents=True)
        with patch("nexus.network.wireguard.PEERS_DIR", peers_dir):
            assert remove_peer(99) is False


class TestGenerateConfig:
    """Test WireGuard config generation."""

    def test_basic_config(self) -> None:
        identity = {
            "private_key": "PRIVKEY",
            "ip": "10.99.0.1",
            "listen_port": 51820,
        }
        peers = [
            {
                "public_key": "PUBKEY_PEER",
                "ip": "10.99.0.2",
                "endpoint": "192.168.1.50:51820",
            }
        ]

        config = generate_wg_config(identity, peers)

        assert "[Interface]" in config
        assert "PrivateKey = PRIVKEY" in config
        assert "Address = 10.99.0.1/24" in config
        assert "ListenPort = 51820" in config
        assert "[Peer]" in config
        assert "PublicKey = PUBKEY_PEER" in config
        assert "AllowedIPs = 10.99.0.2/32" in config
        assert "Endpoint = 192.168.1.50:51820" in config
        assert "PersistentKeepalive = 25" in config

    def test_no_peers(self) -> None:
        identity = {
            "private_key": "PRIVKEY",
            "ip": "10.99.0.1",
            "listen_port": 51820,
        }
        config = generate_wg_config(identity, [])
        assert "[Peer]" not in config

    def test_multiple_peers(self) -> None:
        identity = {
            "private_key": "PRIVKEY",
            "ip": "10.99.0.1",
            "listen_port": 51820,
        }
        peers = [
            {"public_key": "PK1", "ip": "10.99.0.2", "endpoint": "1.1.1.1:51820"},
            {"public_key": "PK2", "ip": "10.99.0.3", "endpoint": "2.2.2.2:51820"},
        ]
        config = generate_wg_config(identity, peers)
        assert config.count("[Peer]") == 2
