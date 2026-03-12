"""Tests for nexus.cli.port_utils — port conflict detection and resolution."""

from __future__ import annotations

import socket

import pytest

from nexus.cli.port_utils import (
    DEFAULT_PORTS,
    VALID_STRATEGIES,
    check_port_available,
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
            s.bind(("127.0.0.1", 0))
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
    def test_returns_preferred_when_free(self) -> None:
        """If preferred port is free, return it directly."""
        # Get a known-free port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            _, port = s.getsockname()
        result = find_free_port(port)
        assert result == port

    def test_skips_occupied_port(self) -> None:
        """If preferred port is occupied, return the next free one."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", 0))
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
            s.bind(("127.0.0.1", 0))
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
            "nexus.cli.port_utils.check_port_available", lambda port, host="127.0.0.1": True
        )
        ports = {"http": 2026, "postgres": 5432}
        resolved, messages = resolve_ports(ports, strategy="auto")
        assert resolved == ports
        assert messages == []

    def test_auto_strategy_picks_next(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Auto strategy should pick the next free port."""

        # Port 2026 occupied, 2027 free
        def mock_available(port: int, host: str = "127.0.0.1") -> bool:
            return port != 2026

        monkeypatch.setattr("nexus.cli.port_utils.check_port_available", mock_available)

        # Also mock find_free_port to return deterministic result
        monkeypatch.setattr(
            "nexus.cli.port_utils.find_free_port",
            lambda preferred, host="127.0.0.1": preferred,  # 2027
        )

        ports = {"http": 2026}
        resolved, messages = resolve_ports(ports, strategy="auto")
        assert resolved["http"] == 2027  # preferred + 1
        assert len(messages) == 1
        assert "2026" in messages[0]

    def test_fail_strategy_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fail strategy should exit when a port is occupied."""
        monkeypatch.setattr(
            "nexus.cli.port_utils.check_port_available", lambda port, host="127.0.0.1": False
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

    def test_valid_strategies_constant(self) -> None:
        assert VALID_STRATEGIES == ("auto", "prompt", "fail")

    def test_default_ports_has_expected_keys(self) -> None:
        assert "http" in DEFAULT_PORTS
        assert "grpc" in DEFAULT_PORTS
        assert "postgres" in DEFAULT_PORTS
        assert "dragonfly" in DEFAULT_PORTS
        assert "zoekt" in DEFAULT_PORTS
