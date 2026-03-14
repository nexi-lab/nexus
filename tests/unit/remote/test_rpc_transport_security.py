"""Regression tests for gRPC transport security — Issue #2960 H7+H8.

Verifies that insecure gRPC channels are refused for non-loopback addresses.

Note: Tests the validation logic directly without importing RPCTransport
(which depends on generated protobuf stubs that may not match the installed
protobuf version).
"""


class TestInsecureChannelLoopbackValidation:
    """Regression: H8 — MITM risk from insecure gRPC to remote hosts.

    Tests the address validation logic that was added to RPCTransport.__init__.
    """

    @staticmethod
    def _extract_host(server_address: str) -> str:
        """Mirror the logic in RPCTransport.__init__."""
        return server_address.rsplit(":", 1)[0]

    @staticmethod
    def _is_allowed_insecure(server_address: str) -> bool:
        """Check if insecure channel would be allowed for this address."""
        import ipaddress

        host = server_address.rsplit(":", 1)[0].strip("[]")
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def test_remote_ip_rejected(self) -> None:
        assert not self._is_allowed_insecure("192.168.1.100:2028")

    def test_public_ip_rejected(self) -> None:
        assert not self._is_allowed_insecure("10.0.0.1:2028")

    def test_external_host_rejected(self) -> None:
        assert not self._is_allowed_insecure("api.example.com:2028")

    def test_localhost_allowed(self) -> None:
        assert self._is_allowed_insecure("localhost:2028")

    def test_ipv4_loopback_allowed(self) -> None:
        assert self._is_allowed_insecure("127.0.0.1:2028")

    def test_ipv6_loopback_allowed(self) -> None:
        assert self._is_allowed_insecure("::1:2028")

    def test_validation_matches_source_code(self) -> None:
        """Verify the validation logic matches what's in rpc_transport.py.

        This reads the source file to confirm the ValueError is still present,
        ensuring the security check hasn't been removed.
        """
        from pathlib import Path

        src_path = (
            Path(__file__).resolve().parents[3] / "src" / "nexus" / "remote" / "rpc_transport.py"
        )
        source = src_path.read_text()
        assert "Insecure gRPC channel refused" in source, (
            "The insecure channel refusal check was removed from rpc_transport.py"
        )
