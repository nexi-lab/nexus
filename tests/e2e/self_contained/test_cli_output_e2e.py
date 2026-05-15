"""E2E tests for CLI unified output infrastructure (Issue #2806).

Tests run against a REAL local NexusFS with a temp data directory.

Two test modes:
  1. **Local** — each CLI command subprocess cold-starts NexusFS (tests the
     standalone experience, ~2s connect overhead per invocation).
  2. **Remote** — a ``nexusd`` daemon is started once; CLI commands hit
     it via ``--remote-url`` (tests the production experience, fast connect).

Validates:
- JSON envelope structure with real data
- Timing accuracy (phases > 0ms)
- --fields filtering with real results
- --quiet suppression
- -vvv request_id inclusion
- Human output formatting
- Performance: server phase completes under reasonable thresholds

No mocks. Every command hits a real NexusFS instance.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

import nexus

# After PR #2842, CLI commands are remote-only (require --remote-url).
# Local-mode CLI tests that seed a data dir and run CLI directly no longer work.
_cli_local_skip = pytest.mark.skip(
    reason="CLI is remote-only after PR #2842; local-mode CLI tests disabled"
)

# Ensure subprocess uses the worktree source (not the venv-installed package)
_WORKTREE_SRC = str(Path(__file__).resolve().parents[3] / "src")
_SUBPROCESS_ENV = {
    **os.environ,
    "NEXUS_NO_AUTO_JSON": "1",
    "PYTHONPATH": _WORKTREE_SRC + os.pathsep + os.environ.get("PYTHONPATH", ""),
}


def _find_free_port() -> int:
    """Find a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def nexus_data_dir(tmp_path: Path) -> str:
    """Create a temp data dir and return its path."""
    data_dir = tmp_path / "nexus-data"
    data_dir.mkdir()
    return str(data_dir)


@pytest.fixture()
async def seeded_data_dir(nexus_data_dir: str) -> str:
    """Create a NexusFS instance, seed it with test files, then return the data dir.

    The NexusFS is explicitly closed and the reference deleted + GC'd
    to release the redb exclusive file lock before tests run.
    """
    import gc

    nx = nexus.connect(config={"data_dir": nexus_data_dir})
    nx.mkdir("/workspace", exist_ok=True)
    nx.mkdir("/workspace/src", exist_ok=True)
    nx.mkdir("/workspace/docs", exist_ok=True)

    nx.write("/workspace/src/main.py", b'# TODO: implement\nprint("hello")\n')
    nx.write("/workspace/src/utils.py", b"def helper():\n    return 42\n")
    nx.write("/workspace/docs/README.md", b"# Project\nThis is a test project.\n")
    nx.write("/workspace/data.txt", b"line one\nline two\nline three\n")
    nx.close()
    del nx
    gc.collect()
    return nexus_data_dir


def _run_nexus(args: list[str], data_dir: str) -> subprocess.CompletedProcess[str]:
    """Run a nexus CLI command via subprocess.

    Uses ``python -c`` with the CLI entry point to ensure the correct
    interpreter and avoids redb lock conflicts with the seeding fixture.
    """
    env = {**_SUBPROCESS_ENV, "NEXUS_DATA_DIR": data_dir}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from nexus.cli.main import main; main()",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    return result


def _parse_json(result: subprocess.CompletedProcess[str]) -> dict:
    """Parse JSON from stdout, with helpful error on failure."""
    assert result.returncode == 0, (
        f"Command failed (exit {result.returncode}):\n"
        f"stdout: {result.stdout[:500]}\n"
        f"stderr: {result.stderr[:500]}"
    )
    return json.loads(result.stdout)


# =========================================================================
# ls (list_files)
# =========================================================================


@_cli_local_skip
class TestLsE2E:
    """nexus ls against real NexusFS."""

    def test_ls_json_returns_real_files(self, seeded_data_dir: str) -> None:
        result = _run_nexus(["ls", "/workspace", "--json"], seeded_data_dir)
        output = _parse_json(result)
        paths = [e["path"] for e in output["data"]]
        assert "/workspace/data.txt" in paths
        assert "/workspace/src" in paths or "/workspace/src/" in paths

    def test_ls_json_timing_is_real(self, seeded_data_dir: str) -> None:
        result = _run_nexus(["ls", "/workspace", "--json"], seeded_data_dir)
        output = _parse_json(result)

        timing = output["_timing"]
        assert timing["total_ms"] > 0
        assert timing["phases"]["server"] > 0
        assert timing["phases"]["connect"] > 0

    def test_ls_json_fields_filter(self, seeded_data_dir: str) -> None:
        result = _run_nexus(["ls", "/workspace", "--json", "--fields", "path"], seeded_data_dir)
        output = _parse_json(result)

        for entry in output["data"]:
            assert list(entry.keys()) == ["path"]

    def test_ls_quiet(self, seeded_data_dir: str) -> None:
        result = _run_nexus(["ls", "/workspace", "--quiet"], seeded_data_dir)
        assert result.returncode == 0
        assert result.stdout == ""

    def test_ls_vvv_has_request_id(self, seeded_data_dir: str) -> None:
        result = _run_nexus(["ls", "/workspace", "--json", "-vvv"], seeded_data_dir)
        output = _parse_json(result)
        assert "_request_id" in output
        assert len(output["_request_id"]) == 32

    def test_ls_human_output(self, seeded_data_dir: str) -> None:
        result = _run_nexus(["ls", "/workspace"], seeded_data_dir)
        assert result.returncode == 0
        assert "data.txt" in result.stdout

    def test_ls_recursive(self, seeded_data_dir: str) -> None:
        result = _run_nexus(["ls", "/workspace", "--recursive", "--json"], seeded_data_dir)
        output = _parse_json(result)
        paths = [e["path"] for e in output["data"]]
        assert any("main.py" in p for p in paths)

    def test_ls_long_format(self, seeded_data_dir: str) -> None:
        result = _run_nexus(["ls", "/workspace", "--long"], seeded_data_dir)
        assert result.returncode == 0
        # Long format shows a table with size info
        assert "bytes" in result.stdout or "file" in result.stdout.lower()

    def test_ls_performance(self, seeded_data_dir: str) -> None:
        """ls server phase should be under 500ms for a small directory."""
        result = _run_nexus(["ls", "/workspace", "--json"], seeded_data_dir)
        output = _parse_json(result)
        # Check server phase (excludes cold-start connect/import overhead)
        server_ms = output["_timing"]["phases"]["server"]
        assert server_ms < 500, f"ls server phase {server_ms:.0f}ms, expected < 500ms"


# =========================================================================
# tree
# =========================================================================


@_cli_local_skip
class TestTreeE2E:
    """nexus tree against real NexusFS."""

    def test_tree_json(self, seeded_data_dir: str) -> None:
        result = _run_nexus(["tree", "/workspace", "--json"], seeded_data_dir)
        output = _parse_json(result)
        assert output["data"]["root"] == "/workspace"
        assert output["data"]["total_files"] > 0
        assert output["data"]["total_size"] > 0

    def test_tree_human(self, seeded_data_dir: str) -> None:
        result = _run_nexus(["tree", "/workspace"], seeded_data_dir)
        assert result.returncode == 0
        assert "main.py" in result.stdout

    def test_tree_performance(self, seeded_data_dir: str) -> None:
        result = _run_nexus(["tree", "/workspace", "--json"], seeded_data_dir)
        output = _parse_json(result)
        server_ms = output["_timing"]["phases"]["server"]
        assert server_ms < 500, f"tree server phase {server_ms:.0f}ms, expected < 500ms"


# =========================================================================
# cat
# =========================================================================


@_cli_local_skip
class TestCatE2E:
    """nexus cat against real NexusFS."""

    def test_cat_json(self, seeded_data_dir: str) -> None:
        result = _run_nexus(["cat", "/workspace/data.txt", "--json"], seeded_data_dir)
        output = _parse_json(result)
        assert output["data"]["path"] == "/workspace/data.txt"
        assert "line one" in output["data"]["content"]
        assert output["data"]["binary"] is False
        assert output["data"]["size"] > 0

    def test_cat_json_with_metadata(self, seeded_data_dir: str) -> None:
        result = _run_nexus(["cat", "/workspace/data.txt", "--json", "--metadata"], seeded_data_dir)
        output = _parse_json(result)
        assert "metadata" in output["data"]
        assert "content_id" in output["data"]["metadata"]

    def test_cat_human_with_content(self, seeded_data_dir: str) -> None:
        result = _run_nexus(["cat", "/workspace/data.txt"], seeded_data_dir)
        assert result.returncode == 0
        assert "line one" in result.stdout

    def test_cat_python_syntax_highlighting(self, seeded_data_dir: str) -> None:
        """Python files should render without errors."""
        result = _run_nexus(["cat", "/workspace/src/main.py"], seeded_data_dir)
        assert result.returncode == 0
        assert "hello" in result.stdout

    def test_cat_timing(self, seeded_data_dir: str) -> None:
        result = _run_nexus(["cat", "/workspace/data.txt", "--json"], seeded_data_dir)
        output = _parse_json(result)
        assert output["_timing"]["total_ms"] > 0

    def test_cat_performance(self, seeded_data_dir: str) -> None:
        result = _run_nexus(["cat", "/workspace/data.txt", "--json"], seeded_data_dir)
        output = _parse_json(result)
        server_ms = output["_timing"]["phases"]["server"]
        assert server_ms < 500, f"cat server phase {server_ms:.0f}ms, expected < 500ms"


# =========================================================================
# glob
# =========================================================================


@_cli_local_skip
class TestGlobE2E:
    """nexus glob against real NexusFS."""

    def test_glob_json(self, seeded_data_dir: str) -> None:
        result = _run_nexus(["glob", "**/*.py", "/workspace", "--json"], seeded_data_dir)
        output = _parse_json(result)
        paths = [e["path"] for e in output["data"]]
        assert any("main.py" in p for p in paths)
        assert any("utils.py" in p for p in paths)

    def test_glob_no_matches(self, seeded_data_dir: str) -> None:
        result = _run_nexus(["glob", "**/*.xyz", "/workspace", "--json"], seeded_data_dir)
        output = _parse_json(result)
        assert output["data"] == []

    def test_glob_timing(self, seeded_data_dir: str) -> None:
        result = _run_nexus(["glob", "**/*.py", "/workspace", "--json"], seeded_data_dir)
        output = _parse_json(result)
        assert output["_timing"]["total_ms"] > 0

    def test_glob_performance(self, seeded_data_dir: str) -> None:
        result = _run_nexus(["glob", "**/*.py", "/workspace", "--json"], seeded_data_dir)
        output = _parse_json(result)
        server_ms = output["_timing"]["phases"]["server"]
        assert server_ms < 500, f"glob server phase {server_ms:.0f}ms, expected < 500ms"


# =========================================================================
# grep
# =========================================================================


@_cli_local_skip
class TestGrepE2E:
    """nexus grep against real NexusFS."""

    def test_grep_json(self, seeded_data_dir: str) -> None:
        result = _run_nexus(["grep", "TODO", "/workspace", "--json"], seeded_data_dir)
        output = _parse_json(result)
        assert output["data"]["total_matches"] >= 1
        assert output["data"]["files_matched"] >= 1
        # Verify actual match content
        match = output["data"]["matches"][0]
        assert "file" in match
        assert "line" in match
        assert "TODO" in match["content"]

    def test_grep_no_matches(self, seeded_data_dir: str) -> None:
        result = _run_nexus(
            ["grep", "ZZZYYYXXX_NONEXISTENT", "/workspace", "--json"], seeded_data_dir
        )
        output = _parse_json(result)
        assert output["data"] == []

    def test_grep_with_file_pattern(self, seeded_data_dir: str) -> None:
        result = _run_nexus(["grep", "def", "/workspace", "-f", "*.py", "--json"], seeded_data_dir)
        output = _parse_json(result)
        # Should find "def helper()" in utils.py
        assert output["data"]["total_matches"] >= 1

    def test_grep_timing(self, seeded_data_dir: str) -> None:
        result = _run_nexus(["grep", "TODO", "/workspace", "--json"], seeded_data_dir)
        output = _parse_json(result)
        assert output["_timing"]["total_ms"] > 0

    def test_grep_performance(self, seeded_data_dir: str) -> None:
        result = _run_nexus(["grep", "TODO", "/workspace", "--json"], seeded_data_dir)
        output = _parse_json(result)
        server_ms = output["_timing"]["phases"]["server"]
        assert server_ms < 500, f"grep server phase {server_ms:.0f}ms, expected < 500ms"


# =========================================================================
# info
# =========================================================================


@_cli_local_skip
class TestInfoE2E:
    """nexus info against real NexusFS."""

    def test_info_json(self, seeded_data_dir: str) -> None:
        result = _run_nexus(["info", "/workspace/data.txt", "--json"], seeded_data_dir)
        output = _parse_json(result)
        data = output["data"]
        assert data["path"] == "/workspace/data.txt"
        assert data["size"] > 0
        assert data["content_id"] is not None

    def test_info_timing(self, seeded_data_dir: str) -> None:
        result = _run_nexus(["info", "/workspace/data.txt", "--json"], seeded_data_dir)
        output = _parse_json(result)
        assert output["_timing"]["total_ms"] > 0

    def test_info_performance(self, seeded_data_dir: str) -> None:
        result = _run_nexus(["info", "/workspace/data.txt", "--json"], seeded_data_dir)
        output = _parse_json(result)
        server_ms = output["_timing"]["phases"]["server"]
        assert server_ms < 500, f"info server phase {server_ms:.0f}ms, expected < 500ms"


# =========================================================================
# Cross-cutting: JSON envelope consistency
# =========================================================================


@_cli_local_skip
class TestJsonEnvelopeConsistency:
    """All P0 commands should produce the same JSON envelope shape."""

    @pytest.mark.parametrize(
        ("command", "args"),
        [
            ("ls", ["/workspace"]),
            ("tree", ["/workspace"]),
            ("cat", ["/workspace/data.txt"]),
            ("glob", ["**/*.py", "/workspace"]),
            ("grep", ["TODO", "/workspace"]),
            ("info", ["/workspace/data.txt"]),
        ],
        ids=["ls", "tree", "cat", "glob", "grep", "info"],
    )
    def test_envelope_has_data_and_timing(
        self, command: str, args: list[str], seeded_data_dir: str
    ) -> None:
        result = _run_nexus([command, *args, "--json"], seeded_data_dir)
        envelope = _parse_json(result)
        assert "data" in envelope, f"{command}: missing 'data' key"
        assert "_timing" in envelope, f"{command}: missing '_timing' key"
        assert envelope["_timing"]["total_ms"] > 0


# =========================================================================
# Remote mode — daemon-backed tests
# =========================================================================


def _seed_via_server(server_info: dict[str, str]) -> None:
    """Seed test data by writing through the running server (avoids redb lock)."""
    files = {
        "/workspace/src/main.py": '# TODO: implement\nprint("hello")\n',
        "/workspace/src/utils.py": "def helper():\n    return 42\n",
        "/workspace/docs/README.md": "# Project\nThis is a test project.\n",
        "/workspace/data.txt": "line one\nline two\nline three\n",
    }
    for path, content in files.items():
        # gRPC may take a moment to become ready after HTTP is up — retry up to 10s
        for attempt in range(20):
            result = _run_nexus_remote(["write", path, content], server_info)
            if result.returncode == 0:
                break
            if "unavailable" in result.stdout.lower() and attempt < 19:
                time.sleep(0.5)
                continue
            break
        assert result.returncode == 0, (
            f"Failed to seed {path}: stdout={result.stdout[:200]} stderr={result.stderr[:200]}"
        )


@pytest.fixture(scope="module")
def remote_server(tmp_path_factory: pytest.TempPathFactory):  # noqa: ANN201
    """Start a nexusd daemon, seed data, and yield connection info.

    The server is started once per module and shared across all remote tests.
    Data is seeded through the running server to avoid redb lock conflicts.
    """
    data_dir = str(tmp_path_factory.mktemp("remote-nexus-data"))
    http_port = _find_free_port()
    grpc_port = _find_free_port()
    url = f"http://127.0.0.1:{http_port}"

    # Server env: enable gRPC on the chosen port, set data dir via env
    server_env = {
        **_SUBPROCESS_ENV,
        "NEXUS_GRPC_PORT": str(grpc_port),
        "NEXUS_DATA_DIR": data_dir,
    }

    # Start server as a subprocess using nexusd entry point
    nexusd_bin = str(Path(sys.executable).parent / "nexusd")
    server_proc = subprocess.Popen(
        [
            nexusd_bin,
            "--host",
            "127.0.0.1",
            "--port",
            str(http_port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=server_env,
    )

    # Wait for server readiness (poll /healthz/ready)
    ready = False
    for _ in range(60):  # up to 30s
        time.sleep(0.5)
        if server_proc.poll() is not None:
            # Server exited early
            stderr = server_proc.stderr.read().decode() if server_proc.stderr else ""
            pytest.skip(f"Server exited early (code {server_proc.returncode}): {stderr[:300]}")
        try:
            resp = urllib.request.urlopen(f"{url}/healthz/ready", timeout=2)
            if resp.status == 200:
                ready = True
                break
        except Exception:
            continue

    if not ready:
        server_proc.terminate()
        server_proc.wait(timeout=5)
        pytest.skip("Server did not become ready within 30s")

    info = {
        "url": url,
        "data_dir": data_dir,
        "http_port": str(http_port),
        "grpc_port": str(grpc_port),
    }

    # Seed test data through the running server
    _seed_via_server(info)

    yield info

    # Teardown: stop server
    server_proc.send_signal(signal.SIGINT)
    try:
        server_proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server_proc.kill()
        server_proc.wait(timeout=5)


def _run_nexus_remote(
    args: list[str], server_info: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run a nexus CLI command against a remote server."""
    env_args = ["--remote-url", server_info["url"]]
    # Client needs NEXUS_GRPC_PORT to connect to the server's gRPC port
    client_env = {**_SUBPROCESS_ENV, "NEXUS_GRPC_PORT": server_info["grpc_port"]}
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from nexus.cli.main import main; main()",
            *args,
            *env_args,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=client_env,
    )


@pytest.mark.skip(reason="Remote daemon tests are flaky in CI — gRPC server stability (#1483)")
class TestRemoteLsE2E:
    """nexus ls against a running server (remote mode)."""

    def test_ls_json(self, remote_server: dict[str, str]) -> None:
        result = _run_nexus_remote(["ls", "/workspace", "--json"], remote_server)
        output = _parse_json(result)
        paths = [e["path"] for e in output["data"]]
        assert "/workspace/data.txt" in paths

    def test_ls_timing_phases(self, remote_server: dict[str, str]) -> None:
        result = _run_nexus_remote(["ls", "/workspace", "--json"], remote_server)
        output = _parse_json(result)
        assert output["_timing"]["total_ms"] > 0
        assert output["_timing"]["phases"]["connect"] > 0

    def test_ls_performance_total(self, remote_server: dict[str, str]) -> None:
        """With a warm server, total_ms should be reasonable (< 2s including CLI startup)."""
        result = _run_nexus_remote(["ls", "/workspace", "--json"], remote_server)
        output = _parse_json(result)
        total_ms = output["_timing"]["total_ms"]
        assert total_ms < 2000, f"remote ls total {total_ms:.0f}ms, expected < 2000ms"


@pytest.mark.skip(reason="Remote daemon tests are flaky in CI — gRPC server stability (#1483)")
class TestRemoteCatE2E:
    """nexus cat against a running server."""

    def test_cat_json(self, remote_server: dict[str, str]) -> None:
        result = _run_nexus_remote(["cat", "/workspace/data.txt", "--json"], remote_server)
        output = _parse_json(result)
        assert "line one" in output["data"]["content"]
        assert output["data"]["size"] > 0


@pytest.mark.skip(reason="Remote daemon tests are flaky in CI — gRPC server stability (#1483)")
class TestRemoteGrepE2E:
    """nexus grep against a running server."""

    def test_grep_json(self, remote_server: dict[str, str]) -> None:
        result = _run_nexus_remote(["grep", "TODO", "/workspace", "--json"], remote_server)
        output = _parse_json(result)
        assert output["data"]["total_matches"] >= 1


@pytest.mark.skip(reason="Remote daemon tests are flaky in CI — gRPC server stability (#1483)")
class TestRemoteGlobE2E:
    """nexus glob against a running server."""

    def test_glob_json(self, remote_server: dict[str, str]) -> None:
        result = _run_nexus_remote(["glob", "**/*.py", "/workspace", "--json"], remote_server)
        output = _parse_json(result)
        paths = [e["path"] for e in output["data"]]
        assert any("main.py" in p for p in paths)


@pytest.mark.skip(reason="Remote daemon tests are flaky in CI — gRPC server stability (#1483)")
class TestRemoteEnvelopeConsistency:
    """JSON envelope shape across all commands in remote mode."""

    @pytest.mark.parametrize(
        ("command", "args"),
        [
            pytest.param("ls", ["/workspace"], id="ls"),
            pytest.param("tree", ["/workspace"], id="tree"),
            pytest.param("cat", ["/workspace/data.txt"], id="cat"),
            pytest.param("glob", ["**/*.py", "/workspace"], id="glob"),
            pytest.param("grep", ["TODO", "/workspace"], id="grep"),
            pytest.param("info", ["/workspace/data.txt"], id="info"),
        ],
    )
    def test_remote_envelope(
        self, command: str, args: list[str], remote_server: dict[str, str]
    ) -> None:
        result = _run_nexus_remote([command, *args, "--json"], remote_server)
        envelope = _parse_json(result)
        assert "data" in envelope, f"remote {command}: missing 'data'"
        assert "_timing" in envelope, f"remote {command}: missing '_timing'"
        assert envelope["_timing"]["total_ms"] > 0
