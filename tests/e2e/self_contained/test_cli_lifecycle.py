"""E2E tests for CLI infrastructure lifecycle commands (Issue #2807).

Tests ``nexus doctor`` and ``nexus status`` as real subprocess invocations.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Use the console_scripts entry point from the same venv as the test runner.
_NEXUS_BIN = str(Path(sys.executable).parent / "nexus")


def _run_nexus(
    *args: str, timeout: float = 30, env: dict | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``nexus <args>`` as a subprocess and return the result."""
    merged_env = {**os.environ, **(env or {})}
    return subprocess.run(
        [_NEXUS_BIN, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=merged_env,
    )


def _find_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


# ============================================================================
# LOCAL TESTS — no Docker required
# ============================================================================


class TestDoctorE2E:
    """Run ``nexus doctor`` as a real subprocess."""

    def test_doctor_runs_and_exits(self) -> None:
        """Doctor should run all checks and exit cleanly."""
        result = _run_nexus("doctor")
        # Exit 0 = all ok, exit 1 = warnings/errors found — both are valid
        assert result.returncode in (0, 1)
        # Human-readable output contains "checks" somewhere
        combined = result.stdout + result.stderr
        assert "check" in combined.lower(), f"Expected 'check' in output: {combined}"

    def test_doctor_json_output(self) -> None:
        """--json flag should produce valid JSON with all 5 categories."""
        result = _run_nexus("doctor", "--json")
        assert result.returncode in (0, 1)
        data = json.loads(result.stdout)
        expected_categories = {"connectivity", "storage", "federation", "security", "dependencies"}
        assert set(data.keys()) == expected_categories
        # Each category should be a list of check results
        for category, checks in data.items():
            assert isinstance(checks, list), f"{category} should be a list"
            for check in checks:
                assert "name" in check
                assert "status" in check
                assert check["status"] in ("ok", "warning", "error")

    def test_doctor_json_check_count(self) -> None:
        """Should have at least 10 checks across all categories."""
        result = _run_nexus("doctor", "--json")
        data = json.loads(result.stdout)
        total = sum(len(checks) for checks in data.values())
        assert total >= 10, f"Expected at least 10 checks, got {total}"

    def test_doctor_fix_flag(self) -> None:
        """--fix flag should be accepted (may or may not fix things)."""
        result = _run_nexus("doctor", "--fix")
        assert result.returncode in (0, 1)


class TestStatusE2E:
    """Run ``nexus status`` as a real subprocess."""

    def test_status_json_no_server(self) -> None:
        """Status against a port with no server should return JSON with server_reachable=false."""
        port = _find_free_port()
        result = _run_nexus("status", "--json", "--url", f"http://127.0.0.1:{port}")
        assert result.returncode == 0, f"status exited {result.returncode}: {result.stderr}"
        data = json.loads(result.stdout)
        assert data["server_reachable"] is False
        assert data["server_health"] is None


# ============================================================================
# DOCKER TESTS — require Docker daemon running
# ============================================================================

_docker_available = False
try:
    _docker_result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
    _docker_available = _docker_result.returncode == 0
except (FileNotFoundError, subprocess.TimeoutExpired):
    pass

docker_required = pytest.mark.skipif(
    not _docker_available,
    reason="Docker daemon not available",
)


@pytest.mark.skip(
    reason="up/down/logs moved to nexusd after PR #2842; nexus CLI no longer has these commands"
)
@docker_required
class TestUpDownE2E:
    """Test ``nexus up`` and ``nexus down`` against real Docker.

    These tests use ``--profile cache`` (lightest service) to avoid
    conflicting with any existing running stack.
    """

    @pytest.fixture(autouse=True)
    def _ensure_down(self) -> None:
        """Ensure the cache profile stack is down before and after each test."""
        _run_nexus("down", "--profile", "cache", timeout=30)
        # Also stop any container already on port 6379 (e.g. from another project)
        self._stop_port_6379()
        yield
        _run_nexus("down", "--profile", "cache", timeout=30)

    @staticmethod
    def _stop_port_6379() -> None:
        """Stop any Docker container bound to port 6379."""
        result = subprocess.run(
            ["docker", "ps", "--filter", "publish=6379", "-q"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for cid in result.stdout.strip().splitlines():
            if cid:
                subprocess.run(["docker", "stop", cid], capture_output=True, timeout=30)

    def test_up_and_down_cache_profile(self) -> None:
        """Bring up the cache profile (Dragonfly), verify it starts, then bring it down."""

        result = _run_nexus("up", "--profile", "cache", timeout=120)
        combined = result.stdout + result.stderr
        assert result.returncode == 0, f"nexus up failed: {combined}"

        # Give Docker a moment to fully start
        time.sleep(3)

        # Bring it down
        result = _run_nexus("down", "--profile", "cache", timeout=30)
        combined = result.stdout + result.stderr
        assert result.returncode == 0, f"nexus down failed: {combined}"

    def test_up_invalid_profile(self) -> None:
        """An invalid profile should fail with a descriptive error."""
        result = _run_nexus("up", "--profile", "nonexistent", timeout=15)
        assert result.returncode != 0
        combined = (result.stdout + result.stderr).lower()
        assert "nonexistent" in combined or "unknown" in combined


@pytest.mark.skip(
    reason="logs moved to nexusd after PR #2842; nexus CLI no longer has this command"
)
@docker_required
class TestLogsE2E:
    """Test ``nexus logs`` against real Docker."""

    def test_logs_no_follow(self) -> None:
        """``nexus logs --no-follow --tail 5`` should exit immediately."""
        result = _run_nexus("logs", "--no-follow", "--tail", "5", timeout=15)
        # Should exit cleanly regardless of whether services are running
        assert result.returncode in (0, 1)  # 0 if containers exist, 1 if not
