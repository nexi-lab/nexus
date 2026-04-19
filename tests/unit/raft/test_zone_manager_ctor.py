"""Constructor compatibility tests for the Python Raft ZoneManager wrapper."""

from nexus.raft.peer_address import hostname_to_node_id
from nexus.raft.zone_manager import _make_py_zone_manager


class _HostnameFirstPyZoneManager:
    def __init__(
        self,
        hostname,
        base_path,
        peers,
        bind_addr="0.0.0.0:2126",
        tls_cert_path=None,
        tls_key_path=None,
        tls_ca_path=None,
        ca_key_path=None,
        join_token_hash=None,
    ):
        self.args = {
            "hostname": hostname,
            "base_path": base_path,
            "peers": peers,
            "bind_addr": bind_addr,
            "tls_cert_path": tls_cert_path,
            "tls_key_path": tls_key_path,
            "tls_ca_path": tls_ca_path,
            "ca_key_path": ca_key_path,
            "join_token_hash": join_token_hash,
        }


class _NodeIdFirstPyZoneManager:
    def __init__(
        self,
        node_id,
        base_path,
        peers,
        bind_addr="0.0.0.0:2126",
        tls_cert_path=None,
        tls_key_path=None,
        tls_ca_path=None,
        ca_key_path=None,
        join_token_hash=None,
    ):
        self.args = {
            "node_id": node_id,
            "base_path": base_path,
            "peers": peers,
            "bind_addr": bind_addr,
            "tls_cert_path": tls_cert_path,
            "tls_key_path": tls_key_path,
            "tls_ca_path": tls_ca_path,
            "ca_key_path": ca_key_path,
            "join_token_hash": join_token_hash,
        }


def test_make_py_zone_manager_supports_hostname_first_binding() -> None:
    mgr = _make_py_zone_manager(
        _HostnameFirstPyZoneManager,
        hostname="nexus-1",
        base_path="/tmp/zones",
        peers=[],
        bind_addr="127.0.0.1:2126",
        tls_cert_path="cert.pem",
        tls_key_path="key.pem",
        tls_ca_path="ca.pem",
        ca_key_path="ca-key.pem",
        join_token_hash="hash",
    )

    assert mgr.args["hostname"] == "nexus-1"
    assert mgr.args["base_path"] == "/tmp/zones"
    assert mgr.args["peers"] == []
    assert mgr.args["bind_addr"] == "127.0.0.1:2126"
    assert mgr.args["join_token_hash"] == "hash"


def test_make_py_zone_manager_supports_node_id_first_binding() -> None:
    mgr = _make_py_zone_manager(
        _NodeIdFirstPyZoneManager,
        hostname="nexus-1",
        base_path="/tmp/zones",
        peers=["2@nexus-2:2126"],
        bind_addr="127.0.0.1:2126",
        tls_cert_path="cert.pem",
        tls_key_path="key.pem",
        tls_ca_path="ca.pem",
        ca_key_path="ca-key.pem",
        join_token_hash="hash",
    )

    assert mgr.args["node_id"] == hostname_to_node_id("nexus-1")
    assert mgr.args["base_path"] == "/tmp/zones"
    assert mgr.args["peers"] == ["2@nexus-2:2126"]
    assert mgr.args["bind_addr"] == "127.0.0.1:2126"
    assert mgr.args["join_token_hash"] == "hash"
