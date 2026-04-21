"""Tests for nexus.cli.port_utils — port conflict detection and resolution."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from nexus.cli.port_utils import (
    DEFAULT_PORTS,
    VALID_STRATEGIES,
    check_port_available,
    derive_ports,
    find_free_port,
    resolve_ports,
)

# ---------------------------------------------------------------------------
# check_port_available — real socket tests
# ---------------------------------------------------------------------------


class TestCheckPortAvailable:
    """Tests using real sockets for accuracy."""

    def test_free_port_returns_true(self) -> None:
        """An OS-assigned ephemeral port should be free after release."""
        # Get a free port from the OS
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            _, port = s.getsockname()
        # Port should be free now
        assert check_port_available(port) is True

    def test_occupied_port_returns_false(self) -> None:
        """A port with an active listener should be detected as occupied."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", 0))
            s.listen(1)
            _, port = s.getsockname()
            # Port is occupied — should return False
            assert check_port_available(port) is False

    def test_invalid_port_zero(self) -> None:
        assert check_port_available(0) is False

    def test_invalid_port_negative(self) -> None:
        assert check_port_available(-1) is False

    def test_invalid_port_too_high(self) -> None:
        assert check_port_available(65536) is False

    def test_port_at_boundary_1(self) -> None:
        """Port 1 is valid but likely occupied or requires root."""
        # Just verify it doesn't crash
        result = check_port_available(1)
        assert isinstance(result, bool)

    def test_port_at_boundary_65535(self) -> None:
        result = check_port_available(65535)
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# find_free_port — deterministic logic + real sockets
# ---------------------------------------------------------------------------


class TestFindFreePort:
    def test_returns_preferred_when_free(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If preferred port is free, return it directly."""
        monkeypatch.setattr(
            "nexus.cli.port_utils.check_port_available", lambda port, host="0.0.0.0": True
        )
        result = find_free_port(8000)
        assert result == 8000

    def test_skips_occupied_port(self) -> None:
        """If preferred port is occupied, return the next free one."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", 0))
            s.listen(1)
            _, occupied_port = s.getsockname()
            result = find_free_port(occupied_port)
            assert result > occupied_port

    def test_raises_when_no_port_found(self) -> None:
        """Should raise RuntimeError if max_attempts exhausted."""
        # Use port 65535 with max_attempts=1 — port 65535 might be free
        # but 65536 would overflow. Use an occupied port + max_attempts=1.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", 0))
            s.listen(1)
            _, occupied_port = s.getsockname()
            # Only allow 1 attempt starting from the occupied port
            # This will check only occupied_port and fail
            with pytest.raises(RuntimeError, match="No free port found"):
                find_free_port(occupied_port, max_attempts=1)


# ---------------------------------------------------------------------------
# resolve_ports — algorithm tests (mocked port state)
# ---------------------------------------------------------------------------


class TestResolvePorts:
    def test_all_ports_free(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When all ports are free, return them unchanged."""
        monkeypatch.setattr(
            "nexus.cli.port_utils.check_port_available", lambda port, host="0.0.0.0": True
        )
        ports = {"http": 2026, "postgres": 5432}
        resolved, messages = resolve_ports(ports, strategy="auto")
        assert resolved == ports
        assert messages == []

    def test_auto_strategy_picks_next(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Auto strategy should pick the next free port."""

        # Port 2026 occupied, 2027+ free
        def mock_available(port: int, host: str = "127.0.0.1") -> bool:
            return port != 2026

        monkeypatch.setattr("nexus.cli.port_utils.check_port_available", mock_available)

        ports = {"http": 2026}
        resolved, messages = resolve_ports(ports, strategy="auto")
        assert resolved["http"] == 2027  # next free after 2026
        assert len(messages) == 1
        assert "2026" in messages[0]

    def test_fail_strategy_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fail strategy should exit when a port is occupied."""
        monkeypatch.setattr(
            "nexus.cli.port_utils.check_port_available", lambda port, host="0.0.0.0": False
        )
        with pytest.raises(SystemExit):
            resolve_ports({"http": 2026}, strategy="fail")

    def test_invalid_strategy_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid port strategy"):
            resolve_ports({"http": 2026}, strategy="invalid")

    def test_services_filter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Only check ports for services in the active set."""
        call_log: list[int] = []

        def mock_available(port: int, host: str = "127.0.0.1") -> bool:
            call_log.append(port)
            return True

        monkeypatch.setattr("nexus.cli.port_utils.check_port_available", mock_available)
        ports = {"http": 2026, "postgres": 5432, "dragonfly": 6379}
        resolved, _ = resolve_ports(ports, strategy="auto", services=["http"])
        # Only http port should be checked
        assert 2026 in call_log
        assert 5432 not in call_log
        # All ports should be in the result (non-checked passed through)
        assert resolved == ports

    def test_auto_no_self_conflict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two services conflicting on different ports should not resolve to the same port."""
        # Both 2026 and 2028 occupied, 2027 and 2029 free
        occupied = {2026, 2028}

        def mock_available(port: int, host: str = "127.0.0.1") -> bool:
            return port not in occupied

        monkeypatch.setattr("nexus.cli.port_utils.check_port_available", mock_available)

        ports = {"http": 2026, "grpc": 2028}
        resolved, messages = resolve_ports(ports, strategy="auto")
        # Each resolved port must be unique
        assert resolved["http"] != resolved["grpc"]
        assert len(messages) == 2
        # Both should get the next free ports after their preferred
        assert resolved["http"] == 2027
        assert resolved["grpc"] == 2029  # 2028 occupied, 2027 already claimed → 2029

    def test_valid_strategies_constant(self) -> None:
        assert VALID_STRATEGIES == ("auto", "prompt", "fail")

    def test_default_ports_has_expected_keys(self) -> None:
        assert "http" in DEFAULT_PORTS
        assert "grpc" in DEFAULT_PORTS
        assert "postgres" in DEFAULT_PORTS
        assert "dragonfly" in DEFAULT_PORTS


# ---------------------------------------------------------------------------
# derive_ports — deterministic hash-based port derivation
# ---------------------------------------------------------------------------


class TestDerivePorts:
    def test_returns_all_expected_keys(self) -> None:
        ports = derive_ports("/tmp/project-a")
        assert set(ports.keys()) == {"http", "grpc", "postgres", "dragonfly"}

    def test_deterministic(self) -> None:
        """Same path always produces the same ports."""
        a = derive_ports("/tmp/project-a")
        b = derive_ports("/tmp/project-a")
        assert a == b

    def test_different_paths_differ(self) -> None:
        """Different directories should (almost certainly) get different ports."""
        a = derive_ports("/tmp/project-a")
        b = derive_ports("/tmp/project-b")
        assert a["http"] != b["http"]

    def test_ports_are_contiguous(self) -> None:
        """Ports within a slot should be consecutive."""
        ports = derive_ports("/tmp/test-contiguous")
        base = ports["http"]
        assert ports["grpc"] == base + 1
        assert ports["postgres"] == base + 2
        assert ports["dragonfly"] == base + 3

    def test_ports_in_valid_range(self) -> None:
        """Derived ports should be in 10000–59999."""
        for path in ["/a", "/b/c", "/tmp/nexus-data", "/Users/dev/project"]:
            ports = derive_ports(path)
            for port in ports.values():
                assert 10000 <= port <= 59999, f"Port {port} out of range for {path}"

    def test_resolves_relative_paths(self, tmp_path: Path) -> None:
        """Relative and absolute paths to the same dir produce the same ports."""
        import os

        abs_path = str(tmp_path / "nexus-data")
        # Simulate relative path from within tmp_path
        old_cwd = os.getcwd()
        try:
            os.chdir(str(tmp_path))
            rel_ports = derive_ports("nexus-data")
        finally:
            os.chdir(old_cwd)
        abs_ports = derive_ports(abs_path)
        assert rel_ports == abs_ports
