"""E2E tests for CLI infrastructure lifecycle commands (Issue #2807).

Tests ``nexus doctor`` and ``nexus status`` as real subprocess invocations.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from contextlib import closing
from pathlib import Path

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

    def test_status_json_with_running_server(self, nexus_server: dict) -> None:
        """Status against a live server should return JSON with health data."""
        result = _run_nexus("status", "--json", "--url", nexus_server["base_url"])
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["server_reachable"] is True
        assert data["server_health"] is not None
        assert data["server_health"]["status"] in ("healthy", "degraded")

    def test_status_table_output(self, nexus_server: dict) -> None:
        """Non-JSON status should produce a Rich table with 'Nexus Service Status'."""
        result = _run_nexus("status", "--url", nexus_server["base_url"])
        assert result.returncode == 0
        assert "Nexus Service Status" in result.stdout
