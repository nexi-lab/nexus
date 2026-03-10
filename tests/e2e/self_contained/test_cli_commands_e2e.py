"""E2E tests for CLI command groups (Issue #2812).

Tests run against a REAL nexusd server — no mocks.
Each CLI command is invoked via subprocess and validates:
- Exit code == 0 (or graceful non-zero for expected errors)
- JSON envelope structure when --json is used
- Human output contains expected keywords

NOTE: Requires Rust PyO3 extensions for the daemon to start.
Build with: maturin develop -m rust/nexus_raft/Cargo.toml --features full
Tests are auto-skipped if the daemon fails to start (e.g. no Rust build).
"""

import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest

# Ensure subprocess uses the worktree source
_WORKTREE_SRC = str(Path(__file__).resolve().parents[3] / "src")
_SUBPROCESS_ENV = {
    **os.environ,
    "NEXUS_NO_AUTO_JSON": "1",
    "PYTHONPATH": _WORKTREE_SRC + os.pathsep + os.environ.get("PYTHONPATH", ""),
}


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def remote_server(tmp_path_factory):
    """Start a nexusd daemon and yield connection info."""
    data_dir = str(tmp_path_factory.mktemp("cli-cmds-e2e"))
    http_port = _find_free_port()
    grpc_port = _find_free_port()
    url = f"http://127.0.0.1:{http_port}"

    server_env = {
        **_SUBPROCESS_ENV,
        "NEXUS_GRPC_PORT": str(grpc_port),
        "NEXUS_DATA_DIR": data_dir,
    }

    # Start server via python -c (nexusd binary may not be installed)
    server_proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                f"from nexus.daemon.main import main; "
                f"main(['--host', '127.0.0.1', '--port', '{http_port}', "
                f"'--data-dir', '{data_dir}'])"
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=server_env,
        preexec_fn=os.setsid if sys.platform != "win32" else None,
    )

    # Event-driven readiness: watch stderr for startup complete message
    import threading

    stderr_lines: list[str] = []
    ready_event = threading.Event()

    def _drain(pipe, lines, event=None):
        try:
            for raw in iter(pipe.readline, b""):
                decoded = raw.decode(errors="replace")
                lines.append(decoded)
                if event and "Application startup complete" in decoded:
                    event.set()
        except ValueError:
            pass
        finally:
            pipe.close()

    t_err = threading.Thread(
        target=_drain, args=(server_proc.stderr, stderr_lines, ready_event), daemon=True
    )
    t_out = threading.Thread(target=_drain, args=(server_proc.stdout, []), daemon=True)
    t_err.start()
    t_out.start()

    if not ready_event.wait(timeout=60.0):
        server_proc.terminate()
        t_err.join(timeout=2)
        pytest.skip(f"Server did not start within 60s.\nstderr: {''.join(stderr_lines[-20:])}")

    yield {"url": url, "grpc_port": str(grpc_port)}

    server_proc.send_signal(signal.SIGINT)
    try:
        server_proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server_proc.kill()
        server_proc.wait(timeout=5)


def _run_cli(args: list[str], server_info: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run a nexus CLI command against the remote server."""
    client_env = {**_SUBPROCESS_ENV, "NEXUS_GRPC_PORT": server_info["grpc_port"]}
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from nexus.cli.main import main; main()",
            *args,
            "--remote-url",
            server_info["url"],
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=client_env,
    )


def _parse_json(result: subprocess.CompletedProcess[str]) -> dict:
    """Parse JSON envelope, assert exit_code == 0."""
    assert result.returncode == 0, (
        f"Command failed (exit {result.returncode}):\n"
        f"stdout: {result.stdout[:500]}\nstderr: {result.stderr[:500]}"
    )
    return json.loads(result.stdout)


def _parse_json_lenient(result: subprocess.CompletedProcess[str]) -> dict:
    """Parse JSON from stdout regardless of exit code (for 404-style responses)."""
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


# =========================================================================
# identity
# =========================================================================


class TestIdentityE2E:
    """nexus identity commands against a running server."""

    def test_identity_show_unknown_agent(self, remote_server):
        result = _run_cli(["identity", "show", "nonexistent_agent", "--json"], remote_server)
        # Server may return 404 for nonexistent agent
        output = _parse_json_lenient(result)
        assert "data" in output or "error" in output or result.returncode != 0

    def test_identity_verify_unknown_agent(self, remote_server):
        result = _run_cli(["identity", "verify", "nonexistent_agent", "--json"], remote_server)
        # Server may return 404 for nonexistent agent
        output = _parse_json_lenient(result)
        assert "data" in output or "error" in output or result.returncode != 0


# =========================================================================
# ipc
# =========================================================================


class TestIpcE2E:
    """nexus ipc commands against a running server."""

    def test_ipc_count_unknown_agent(self, remote_server):
        result = _run_cli(["ipc", "count", "nonexistent_agent", "--json"], remote_server)
        # Server may return 404 or count=0
        output = _parse_json_lenient(result)
        assert "data" in output or "error" in output or result.returncode != 0

    def test_ipc_inbox_unknown_agent(self, remote_server):
        result = _run_cli(["ipc", "inbox", "nonexistent_agent", "--json"], remote_server)
        # Server may return 404 or empty inbox
        output = _parse_json_lenient(result)
        assert "data" in output or "error" in output or result.returncode != 0


# =========================================================================
# delegation
# =========================================================================


class TestDelegationE2E:
    """nexus delegation commands against a running server."""

    def test_delegation_list_json(self, remote_server):
        result = _run_cli(["delegation", "list", "--json"], remote_server)
        output = _parse_json(result)
        assert "data" in output

    def test_delegation_list_has_timing(self, remote_server):
        result = _run_cli(["delegation", "list", "--json"], remote_server)
        output = _parse_json(result)
        assert "_timing" in output
        assert output["_timing"]["total_ms"] > 0


# =========================================================================
# reputation
# =========================================================================


class TestReputationE2E:
    """nexus reputation commands against a running server."""

    def test_reputation_show_unknown_agent(self, remote_server):
        result = _run_cli(["reputation", "show", "nonexistent_agent", "--json"], remote_server)
        # Server may return 404 for nonexistent agent
        output = _parse_json_lenient(result)
        assert "data" in output or "error" in output or result.returncode != 0

    def test_reputation_leaderboard_json(self, remote_server):
        result = _run_cli(["reputation", "leaderboard", "--json"], remote_server)
        output = _parse_json(result)
        assert "data" in output


# =========================================================================
# scheduler
# =========================================================================


class TestSchedulerE2E:
    """nexus scheduler commands against a running server."""

    def test_scheduler_status_json(self, remote_server):
        result = _run_cli(["scheduler", "status", "--json"], remote_server)
        output = _parse_json(result)
        assert "data" in output

    def test_scheduler_status_has_timing(self, remote_server):
        result = _run_cli(["scheduler", "status", "--json"], remote_server)
        output = _parse_json(result)
        assert "_timing" in output
        assert output["_timing"]["total_ms"] > 0


# =========================================================================
# graph
# =========================================================================


class TestGraphE2E:
    """nexus graph commands against a running server."""

    def test_graph_search_json(self, remote_server):
        result = _run_cli(["graph", "search", "test", "--json"], remote_server)
        # May return empty results or actual results
        output = _parse_json_lenient(result)
        assert "data" in output or "error" in output or result.returncode != 0

    def test_graph_search_no_crash(self, remote_server):
        """graph search should not crash even with no data."""
        result = _run_cli(["graph", "search", "nonexistent_query_xyz"], remote_server)
        # Command should exit without a Python traceback
        assert "Traceback" not in result.stderr


# =========================================================================
# conflicts
# =========================================================================


class TestConflictsE2E:
    """nexus conflicts commands against a running server."""

    def test_conflicts_list_json(self, remote_server):
        result = _run_cli(["conflicts", "list", "--json"], remote_server)
        output = _parse_json(result)
        assert "data" in output

    def test_conflicts_list_has_timing(self, remote_server):
        result = _run_cli(["conflicts", "list", "--json"], remote_server)
        output = _parse_json(result)
        assert "_timing" in output


# =========================================================================
# manifest
# =========================================================================


class TestManifestE2E:
    """nexus manifest commands against a running server."""

    def test_manifest_list_json(self, remote_server):
        result = _run_cli(["manifest", "list", "--json"], remote_server)
        output = _parse_json(result)
        assert "data" in output

    def test_manifest_list_has_timing(self, remote_server):
        result = _run_cli(["manifest", "list", "--json"], remote_server)
        output = _parse_json(result)
        assert "_timing" in output


# =========================================================================
# secrets-audit
# =========================================================================


class TestSecretsAuditE2E:
    """nexus secrets-audit commands against a running server."""

    def test_secrets_audit_list_json(self, remote_server):
        result = _run_cli(["secrets-audit", "list", "--json"], remote_server)
        output = _parse_json(result)
        assert "data" in output

    def test_secrets_audit_list_has_timing(self, remote_server):
        result = _run_cli(["secrets-audit", "list", "--json"], remote_server)
        output = _parse_json(result)
        assert "_timing" in output


# =========================================================================
# rlm
# =========================================================================


class TestRlmE2E:
    """nexus rlm commands against a running server."""

    def test_rlm_status_json(self, remote_server):
        result = _run_cli(["rlm", "status", "--json"], remote_server)
        output = _parse_json(result)
        assert "data" in output

    def test_rlm_status_has_timing(self, remote_server):
        result = _run_cli(["rlm", "status", "--json"], remote_server)
        output = _parse_json(result)
        assert "_timing" in output


# =========================================================================
# upload
# =========================================================================


class TestUploadE2E:
    """nexus upload commands against a running server."""

    def test_upload_list_json(self, remote_server):
        result = _run_cli(["upload", "list", "--json"], remote_server)
        output = _parse_json(result)
        assert "data" in output

    def test_upload_list_has_timing(self, remote_server):
        result = _run_cli(["upload", "list", "--json"], remote_server)
        output = _parse_json(result)
        assert "_timing" in output


# =========================================================================
# agent (extended commands)
# =========================================================================


class TestAgentExtE2E:
    """nexus agent status (extended) against a running server."""

    def test_agent_status_unknown_agent(self, remote_server):
        result = _run_cli(["agent", "status", "nonexistent_agent", "--json"], remote_server)
        # Server may return 404 for nonexistent agent
        output = _parse_json_lenient(result)
        assert "data" in output or "error" in output or result.returncode != 0

    def test_agent_status_no_crash(self, remote_server):
        """agent status should not produce a Python traceback."""
        result = _run_cli(["agent", "status", "nonexistent_agent"], remote_server)
        assert "Traceback" not in result.stderr


# =========================================================================
# Cross-cutting: JSON envelope consistency for list commands
# =========================================================================


class TestJsonEnvelopeConsistency:
    """All list/status commands should produce a JSON envelope with data + _timing."""

    @pytest.mark.parametrize(
        ("command", "args"),
        [
            pytest.param("delegation", ["list"], id="delegation-list"),
            pytest.param("conflicts", ["list"], id="conflicts-list"),
            pytest.param("manifest", ["list"], id="manifest-list"),
            pytest.param("secrets-audit", ["list"], id="secrets-audit-list"),
            pytest.param("upload", ["list"], id="upload-list"),
            pytest.param("scheduler", ["status"], id="scheduler-status"),
            pytest.param("rlm", ["status"], id="rlm-status"),
            pytest.param("reputation", ["leaderboard"], id="reputation-leaderboard"),
        ],
    )
    def test_envelope_has_data_and_timing(
        self, command: str, args: list[str], remote_server: dict[str, str]
    ) -> None:
        result = _run_cli([command, *args, "--json"], remote_server)
        envelope = _parse_json(result)
        assert "data" in envelope, f"{command} {args}: missing 'data' key"
        assert "_timing" in envelope, f"{command} {args}: missing '_timing' key"
        assert envelope["_timing"]["total_ms"] > 0


# =========================================================================
# Missing --remote-url produces a non-zero exit
# =========================================================================


class TestMissingRemoteUrl:
    """Omitting --remote-url should exit non-zero (CLI is remote-only)."""

    def test_delegation_list_no_url(self):
        """delegation list without --remote-url should fail gracefully."""
        env = {**_SUBPROCESS_ENV, "NEXUS_REMOTE_URL": ""}
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from nexus.cli.main import main; main()",
                "delegation",
                "list",
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert result.returncode != 0
