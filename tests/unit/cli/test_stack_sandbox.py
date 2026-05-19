"""Tests for nexus up --profile sandbox shortcut (Issue #3786).

Verifies:
  1. sandbox profile with workspace invokes nexusd with correct args
  2. sandbox profile with workspace + hub-url + hub-token invokes nexusd with all args
  3. --workspace without --profile sandbox → exit(USAGE_ERROR)
  4. --hub-url without --profile sandbox → exit(USAGE_ERROR)
  5. --hub-token without --profile sandbox → exit(USAGE_ERROR)
  6. --profile sandbox with --hub-url but no --hub-token → exit(USAGE_ERROR)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from nexus.cli.commands.env_cmd import env_cmd
from nexus.cli.commands.stack import up
from nexus.cli.exit_codes import ExitCode
from nexus.cli.state import load_runtime_state, resolve_connection_env


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


class TestSandboxShortcutHappyPath:
    def test_workspace_only_invokes_nexusd(self, runner: CliRunner, tmp_path: Path) -> None:
        """nexus up --profile sandbox --workspace /tmp/ws → nexusd with sandbox args."""
        ws = str(tmp_path / "workspace")
        fake_nexusd = "/usr/local/bin/nexusd"

        mock_proc = MagicMock()
        mock_proc.returncode = 0

        with (
            patch("shutil.which", return_value=fake_nexusd),
            patch("subprocess.run", return_value=mock_proc) as mock_run,
        ):
            result = runner.invoke(up, ["--profile", "sandbox", "--workspace", ws])

        assert result.exit_code == 0, result.output
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == fake_nexusd
        assert "--profile" in call_args
        assert "sandbox" in call_args
        assert "--workspace" in call_args
        assert ws in call_args
        assert "--hub-url" not in call_args
        assert "--hub-token" not in call_args

    def test_all_sandbox_flags_invokes_nexusd(self, runner: CliRunner, tmp_path: Path) -> None:
        """nexus up --profile sandbox --workspace /tmp/ws --hub-url grpc://hub --hub-token tok."""
        ws = str(tmp_path / "workspace")
        hub_url = "grpc://hub.example.com:50051"
        hub_token = "secrettoken123"
        fake_nexusd = "/usr/local/bin/nexusd"

        mock_proc = MagicMock()
        mock_proc.returncode = 0

        with (
            patch("shutil.which", return_value=fake_nexusd),
            patch("subprocess.run", return_value=mock_proc) as mock_run,
        ):
            result = runner.invoke(
                up,
                [
                    "--profile",
                    "sandbox",
                    "--workspace",
                    ws,
                    "--hub-url",
                    hub_url,
                    "--hub-token",
                    hub_token,
                ],
            )

        assert result.exit_code == 0, result.output
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == fake_nexusd
        assert "--profile" in call_args
        assert "sandbox" in call_args
        assert "--workspace" in call_args
        assert ws in call_args
        assert "--hub-url" in call_args
        assert hub_url in call_args
        assert "--hub-token" in call_args
        assert hub_token in call_args

    def test_nexusd_fallback_to_module_when_not_in_path(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """When nexusd not in PATH, falls back to sys.executable -m nexus.daemon.main."""
        ws = str(tmp_path / "workspace")

        mock_proc = MagicMock()
        mock_proc.returncode = 0

        with (
            patch("shutil.which", return_value=None),
            patch("subprocess.run", return_value=mock_proc) as mock_run,
        ):
            result = runner.invoke(up, ["--profile", "sandbox", "--workspace", ws])

        assert result.exit_code == 0, result.output
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == sys.executable
        assert "-m" in call_args
        assert "nexus.daemon.main" in call_args
        assert "--profile" in call_args
        assert "sandbox" in call_args

    def test_hub_url_from_env(self, runner: CliRunner, tmp_path: Path) -> None:
        """NEXUS_HUB_URL + NEXUS_HUB_TOKEN env vars are picked up."""
        ws = str(tmp_path / "workspace")
        fake_nexusd = "/usr/local/bin/nexusd"

        mock_proc = MagicMock()
        mock_proc.returncode = 0

        with (
            patch("shutil.which", return_value=fake_nexusd),
            patch("subprocess.run", return_value=mock_proc) as mock_run,
            patch(
                "os.environ",
                {
                    **__import__("os").environ,
                    "NEXUS_HUB_URL": "grpc://hub.env.example.com:50051",
                    "NEXUS_HUB_TOKEN": "envtoken",
                },
            ),
        ):
            result = runner.invoke(up, ["--profile", "sandbox", "--workspace", ws])

        assert result.exit_code == 0, result.output
        call_args = mock_run.call_args[0][0]
        assert "--hub-url" in call_args
        assert "grpc://hub.env.example.com:50051" in call_args
        assert "--hub-token" in call_args
        assert "envtoken" in call_args


# ---------------------------------------------------------------------------
# Validation failure tests
# ---------------------------------------------------------------------------


class TestSandboxFlagValidation:
    def test_workspace_without_sandbox_profile_errors(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """--workspace without --profile sandbox → exit(USAGE_ERROR)."""
        ws = str(tmp_path / "workspace")
        result = runner.invoke(up, ["--workspace", ws])
        assert result.exit_code == ExitCode.USAGE_ERROR
        assert "sandbox" in result.output.lower()

    def test_hub_url_without_sandbox_profile_errors(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """--hub-url without --profile sandbox → exit(USAGE_ERROR)."""
        result = runner.invoke(up, ["--hub-url", "grpc://hub.example.com:50051"])
        assert result.exit_code == ExitCode.USAGE_ERROR
        assert "sandbox" in result.output.lower()

    def test_hub_token_without_sandbox_profile_errors(self, runner: CliRunner) -> None:
        """--hub-token without --profile sandbox → exit(USAGE_ERROR)."""
        result = runner.invoke(up, ["--hub-token", "mytoken"])
        assert result.exit_code == ExitCode.USAGE_ERROR
        assert "sandbox" in result.output.lower()

    def test_hub_url_without_token_errors(self, runner: CliRunner, tmp_path: Path) -> None:
        """--profile sandbox --hub-url without --hub-token → exit(USAGE_ERROR)."""
        ws = str(tmp_path / "workspace")
        result = runner.invoke(
            up,
            [
                "--profile",
                "sandbox",
                "--workspace",
                ws,
                "--hub-url",
                "grpc://hub.example.com:50051",
            ],
        )
        assert result.exit_code == ExitCode.USAGE_ERROR
        assert "token" in result.output.lower()

    def test_hub_token_without_hub_url_is_valid(self, runner: CliRunner, tmp_path: Path) -> None:
        """--hub-token without --hub-url is allowed (token may be used for future URL)."""
        ws = str(tmp_path / "workspace")
        fake_nexusd = "/usr/local/bin/nexusd"

        mock_proc = MagicMock()
        mock_proc.returncode = 0

        with (
            patch("shutil.which", return_value=fake_nexusd),
            patch("subprocess.run", return_value=mock_proc),
        ):
            result = runner.invoke(
                up,
                [
                    "--profile",
                    "sandbox",
                    "--workspace",
                    ws,
                    "--hub-token",
                    "tok",
                ],
            )

        # This is allowed — hub-token without hub-url is not an error
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Issue #4144 — sandbox `up` persists connection state for env/status/run
# ---------------------------------------------------------------------------


def _invoke_sandbox_up(
    runner: CliRunner,
    tmp_path: Path,
    args: list[str],
    env: dict[str, str] | None = None,
) -> tuple[object, MagicMock]:
    """Invoke `nexus up --profile sandbox ...` with subprocess.run mocked.

    Runs inside an isolated tmp cwd so the written nexus.yaml does not
    touch the repo. Returns (CliRunner result, mock_run).
    """
    fake_nexusd = "/usr/local/bin/nexusd"
    mock_proc = MagicMock()
    mock_proc.returncode = 0

    base_env = {"PATH": "/usr/bin"}
    if env:
        base_env.update(env)

    with (
        runner.isolated_filesystem(temp_dir=tmp_path),
        patch("shutil.which", return_value=fake_nexusd),
        patch("subprocess.run", return_value=mock_proc) as mock_run,
        patch.dict("os.environ", base_env, clear=True),
    ):
        result = runner.invoke(up, ["--profile", "sandbox", *args])
    return result, mock_run


class TestSandboxStatePersistence:
    """#4144: sandbox `up` passes host/port/data-dir and persists state."""

    def test_host_port_data_dir_passed_through_to_nexusd(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        ws = str(tmp_path / "ws")
        data_dir = str(tmp_path / "ddir")
        result, mock_run = _invoke_sandbox_up(
            runner,
            tmp_path,
            [
                "--workspace",
                ws,
                "--host",
                "127.0.0.1",
                "--port",
                "3030",
                "--data-dir",
                data_dir,
            ],
        )
        assert result.exit_code == 0, result.output
        argv = mock_run.call_args[0][0]
        assert "--profile" in argv and "sandbox" in argv
        assert "--workspace" in argv and ws in argv
        assert argv[argv.index("--host") + 1] == "127.0.0.1"
        assert argv[argv.index("--port") + 1] == "3030"
        assert argv[argv.index("--data-dir") + 1] == data_dir

    def test_state_file_persisted_with_resolved_ports(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        ws = str(tmp_path / "ws")
        data_dir = str(tmp_path / "ddir")
        result, _ = _invoke_sandbox_up(
            runner,
            tmp_path,
            ["--workspace", ws, "--host", "127.0.0.1", "--port", "3030", "--data-dir", data_dir],
        )
        assert result.exit_code == 0, result.output

        state = load_runtime_state(data_dir)
        assert state, f"state.json not written to {data_dir}"
        assert state["ports"]["http"] == 3030
        assert state["ports"]["grpc"] == 3032  # http + 2 (mirrors nexusd)
        assert state["profile"] == "sandbox"
        assert state["workspace"] == ws

        env = resolve_connection_env({}, state)
        assert env["NEXUS_PROFILE"] == "sandbox"
        assert env["NEXUS_WORKSPACE"] == ws
        assert env["NEXUS_GRPC_PORT"] == "3032"
        assert ":3030" in env["NEXUS_URL"]
        assert ":3032" in env["NEXUS_GRPC_HOST"]

    def test_grpc_port_env_override_mirrors_nexusd(self, runner: CliRunner, tmp_path: Path) -> None:
        ws = str(tmp_path / "ws")
        data_dir = str(tmp_path / "ddir")
        # nexusd rule: explicit --port overrides NEXUS_GRPC_PORT, so test
        # the env-override path WITHOUT an explicit --port (default 2026).
        result, _ = _invoke_sandbox_up(
            runner,
            tmp_path,
            ["--workspace", ws, "--data-dir", data_dir],
            env={"NEXUS_GRPC_PORT": "9999"},
        )
        assert result.exit_code == 0, result.output
        state = load_runtime_state(data_dir)
        assert state["ports"]["http"] == 2026
        assert state["ports"]["grpc"] == 9999

    def test_explicit_port_overrides_grpc_env(self, runner: CliRunner, tmp_path: Path) -> None:
        ws = str(tmp_path / "ws")
        data_dir = str(tmp_path / "ddir")
        result, _ = _invoke_sandbox_up(
            runner,
            tmp_path,
            ["--workspace", ws, "--port", "4000", "--data-dir", data_dir],
            env={"NEXUS_GRPC_PORT": "9999"},
        )
        assert result.exit_code == 0, result.output
        state = load_runtime_state(data_dir)
        assert state["ports"]["http"] == 4000
        assert state["ports"]["grpc"] == 4002  # explicit --port wins over env

    def test_hub_token_never_persisted(self, runner: CliRunner, tmp_path: Path) -> None:
        ws = str(tmp_path / "ws")
        data_dir = str(tmp_path / "ddir")
        secret = "SUPERSECRETHUBTOKEN"
        hub_url = "grpc://hub.example.com:50051"

        with runner.isolated_filesystem(temp_dir=tmp_path):
            fake_nexusd = "/usr/local/bin/nexusd"
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            with (
                patch("shutil.which", return_value=fake_nexusd),
                patch("subprocess.run", return_value=mock_proc),
                patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True),
            ):
                result = runner.invoke(
                    up,
                    [
                        "--profile",
                        "sandbox",
                        "--workspace",
                        ws,
                        "--data-dir",
                        data_dir,
                        "--hub-url",
                        hub_url,
                        "--hub-token",
                        secret,
                    ],
                )
            assert result.exit_code == 0, result.output

            # grep every persisted artifact for the secret
            written: list[Path] = [Path(data_dir) / ".state.json"]
            written += list(Path(".").glob("nexus.y*ml"))
            for f in written:
                assert f.exists(), f
                blob = f.read_text()
                assert secret not in blob, f"hub token leaked into {f}"
            # hub-url MAY be recorded
            state = load_runtime_state(data_dir)
            assert "hub-token" not in json.dumps(state)
            assert secret not in json.dumps(state)

    def test_does_not_clobber_existing_nexus_yaml(self, runner: CliRunner, tmp_path: Path) -> None:
        ws = str(tmp_path / "ws")
        data_dir = str(tmp_path / "ddir")
        fake_nexusd = "/usr/local/bin/nexusd"
        mock_proc = MagicMock()
        mock_proc.returncode = 0

        sentinel = "project_name: my-precious-existing-project\n"
        with runner.isolated_filesystem(temp_dir=tmp_path):
            Path("nexus.yaml").write_text(sentinel)
            with (
                patch("shutil.which", return_value=fake_nexusd),
                patch("subprocess.run", return_value=mock_proc),
                patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True),
            ):
                result = runner.invoke(
                    up,
                    ["--profile", "sandbox", "--workspace", ws, "--data-dir", data_dir],
                )
            assert result.exit_code == 0, result.output
            # Existing config must be byte-identical (never clobbered)
            assert Path("nexus.yaml").read_text() == sentinel

    def test_followup_env_discovers_sandbox_state(self, runner: CliRunner, tmp_path: Path) -> None:
        """After sandbox `up`, `nexus env` (same cwd) emits the conn vars."""
        ws = str(tmp_path / "ws")
        data_dir = str(tmp_path / "ddir")
        fake_nexusd = "/usr/local/bin/nexusd"
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        secret = "SUPERSECRETHUBTOKEN"

        with runner.isolated_filesystem(temp_dir=tmp_path):
            with (
                patch("shutil.which", return_value=fake_nexusd),
                patch("subprocess.run", return_value=mock_proc),
                patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True),
            ):
                up_result = runner.invoke(
                    up,
                    [
                        "--profile",
                        "sandbox",
                        "--workspace",
                        ws,
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "3030",
                        "--data-dir",
                        data_dir,
                        "--hub-url",
                        "grpc://hub:50051",
                        "--hub-token",
                        secret,
                    ],
                )
            assert up_result.exit_code == 0, up_result.output

            env_result = runner.invoke(env_cmd, ["--json"])
            assert env_result.exit_code == 0, env_result.output
            emitted = json.loads(env_result.output)
            assert emitted["NEXUS_PROFILE"] == "sandbox"
            assert emitted["NEXUS_WORKSPACE"] == ws
            assert ":3030" in emitted["NEXUS_URL"]
            assert emitted["NEXUS_GRPC_PORT"] == "3032"
            assert "NEXUS_GRPC_HOST" in emitted
            assert secret not in env_result.output


# ---------------------------------------------------------------------------
# Issue #4144 review fixes
# ---------------------------------------------------------------------------


class TestDataDirSingleSourceOfTruth:
    """BLOCKER 1: when --data-dir is omitted, the CLI must persist state
    where nexusd (launched with --profile sandbox) actually reads it.

    The sandbox connect path resolves data_dir via
    ``nexus.config._apply_sandbox_defaults`` to ``~/.nexus/sandbox`` (NOT
    ``~/.nexus/data``, NOT ``./nexus-data``) unless --data-dir or
    $NEXUS_DATA_DIR is set. The CLI must (a) write .state.json there and
    (b) pass that exact path to the nexusd argv.
    """

    def test_omitted_data_dir_resolves_to_sandbox_home(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        ws = str(tmp_path / "ws")
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        expected = str(fake_home / ".nexus" / "sandbox")

        fake_nexusd = "/usr/local/bin/nexusd"
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        with (
            runner.isolated_filesystem(temp_dir=tmp_path),
            patch("shutil.which", return_value=fake_nexusd),
            patch("subprocess.run", return_value=mock_proc) as mock_run,
            patch.dict("os.environ", {"PATH": "/usr/bin", "HOME": str(fake_home)}, clear=True),
        ):
            result = runner.invoke(up, ["--profile", "sandbox", "--workspace", ws])
            assert result.exit_code == 0, result.output

            # (a) nexusd argv carries the resolved --data-dir
            argv = mock_run.call_args[0][0]
            assert "--data-dir" in argv
            assert argv[argv.index("--data-dir") + 1] == expected

            # (b) .state.json written at the resolved location
            state = load_runtime_state(expected)
            assert state, f".state.json not written at {expected}"
            assert state["profile"] == "sandbox"

    def test_omitted_data_dir_honors_nexus_data_dir_env(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """$NEXUS_DATA_DIR wins over the sandbox default (matches daemon)."""
        ws = str(tmp_path / "ws")
        env_dir = str(tmp_path / "envdir")
        fake_home = tmp_path / "home"
        fake_home.mkdir()

        fake_nexusd = "/usr/local/bin/nexusd"
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        with (
            runner.isolated_filesystem(temp_dir=tmp_path),
            patch("shutil.which", return_value=fake_nexusd),
            patch("subprocess.run", return_value=mock_proc) as mock_run,
            patch.dict(
                "os.environ",
                {"PATH": "/usr/bin", "HOME": str(fake_home), "NEXUS_DATA_DIR": env_dir},
                clear=True,
            ),
        ):
            result = runner.invoke(up, ["--profile", "sandbox", "--workspace", ws])
            assert result.exit_code == 0, result.output
            argv = mock_run.call_args[0][0]
            assert argv[argv.index("--data-dir") + 1] == env_dir
            state = load_runtime_state(env_dir)
            assert state and state["profile"] == "sandbox"

    def test_explicit_data_dir_always_on_argv(self, runner: CliRunner, tmp_path: Path) -> None:
        ws = str(tmp_path / "ws")
        ddir = str(tmp_path / "explicit")
        result, mock_run = _invoke_sandbox_up(
            runner, tmp_path, ["--workspace", ws, "--data-dir", ddir]
        )
        assert result.exit_code == 0, result.output
        argv = mock_run.call_args[0][0]
        assert argv[argv.index("--data-dir") + 1] == ddir


class TestSandboxFailureRollback:
    """IMPORTANT 2: roll back our own .state.json + minimal nexus.yaml when
    the daemon exits non-zero; never delete a pre-existing user config."""

    def test_state_and_created_yaml_removed_on_daemon_failure(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        ws = str(tmp_path / "ws")
        ddir = str(tmp_path / "ddir")
        fake_nexusd = "/usr/local/bin/nexusd"
        mock_proc = MagicMock()
        mock_proc.returncode = 1  # daemon failed to start

        with runner.isolated_filesystem(temp_dir=tmp_path):
            with (
                patch("shutil.which", return_value=fake_nexusd),
                patch("subprocess.run", return_value=mock_proc),
                patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True),
            ):
                result = runner.invoke(
                    up, ["--profile", "sandbox", "--workspace", ws, "--data-dir", ddir]
                )
            assert result.exit_code == 1, result.output
            assert not (Path(ddir) / ".state.json").exists(), "stale .state.json left behind"
            assert not Path("nexus.yaml").exists(), "stale minimal nexus.yaml left behind"

    def test_preexisting_yaml_not_removed_on_daemon_failure(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        ws = str(tmp_path / "ws")
        ddir = str(tmp_path / "ddir")
        fake_nexusd = "/usr/local/bin/nexusd"
        mock_proc = MagicMock()
        mock_proc.returncode = 1

        sentinel = "project_name: my-precious-existing-project\n"
        with runner.isolated_filesystem(temp_dir=tmp_path):
            Path("nexus.yaml").write_text(sentinel)
            with (
                patch("shutil.which", return_value=fake_nexusd),
                patch("subprocess.run", return_value=mock_proc),
                patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True),
            ):
                result = runner.invoke(
                    up, ["--profile", "sandbox", "--workspace", ws, "--data-dir", ddir]
                )
            assert result.exit_code == 1, result.output
            # Pre-existing user config must survive a daemon failure.
            assert Path("nexus.yaml").exists()
            assert Path("nexus.yaml").read_text() == sentinel
            # Our .state.json is still rolled back (we always wrote it).
            assert not (Path(ddir) / ".state.json").exists()


class TestSandboxOptionalFlagValidation:
    """MINOR 5: --host/--port/--data-dir are sandbox-only; reject them
    (only when explicitly set) on the Docker path."""

    def test_port_without_sandbox_profile_errors(self, runner: CliRunner) -> None:
        result = runner.invoke(up, ["--port", "3030"])
        assert result.exit_code == ExitCode.USAGE_ERROR
        assert "sandbox" in result.output.lower()

    def test_host_without_sandbox_profile_errors(self, runner: CliRunner) -> None:
        result = runner.invoke(up, ["--host", "127.0.0.1"])
        assert result.exit_code == ExitCode.USAGE_ERROR
        assert "sandbox" in result.output.lower()

    def test_data_dir_without_sandbox_profile_errors(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        result = runner.invoke(up, ["--data-dir", str(tmp_path / "d")])
        assert result.exit_code == ExitCode.USAGE_ERROR
        assert "sandbox" in result.output.lower()


class TestEmptyNexusYamlBehavior:
    """MINOR 7(a): an empty nexus.yaml (parses to {}) is treated as ABSENT
    — the minimal sandbox config is written. Lock this current behavior."""

    def test_empty_yaml_is_overwritten_with_minimal_config(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        ws = str(tmp_path / "ws")
        ddir = str(tmp_path / "ddir")
        fake_nexusd = "/usr/local/bin/nexusd"
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        with runner.isolated_filesystem(temp_dir=tmp_path):
            Path("nexus.yaml").write_text("")  # empty → parses to {}
            with (
                patch("shutil.which", return_value=fake_nexusd),
                patch("subprocess.run", return_value=mock_proc),
                patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True),
            ):
                result = runner.invoke(
                    up, ["--profile", "sandbox", "--workspace", ws, "--data-dir", ddir]
                )
            assert result.exit_code == 0, result.output
            written = yaml.safe_load(Path("nexus.yaml").read_text())
            assert written is not None, "empty yaml should be replaced by minimal config"
            assert written["profile"] == "sandbox"
            assert written["data_dir"] == ddir


class TestSandboxStateDictShape:
    """MINOR 7(b): lock the EXACT state dict shape stack.py produces so
    the consumer-side integration test (which hand-writes state) stays
    aligned with the producer."""

    def test_exact_state_keys(self, runner: CliRunner, tmp_path: Path) -> None:
        ws = str(tmp_path / "ws")
        ddir = str(tmp_path / "ddir")
        result, _ = _invoke_sandbox_up(
            runner,
            tmp_path,
            ["--workspace", ws, "--host", "127.0.0.1", "--port", "3030", "--data-dir", ddir],
        )
        assert result.exit_code == 0, result.output
        state = load_runtime_state(ddir)
        # version/started_at are added by save_runtime_state; the
        # producer-controlled keys must be exactly these:
        producer_keys = set(state) - {"version", "started_at"}
        assert producer_keys == {"profile", "workspace", "ports", "grpc_host"}
        assert set(state["ports"]) == {"http", "grpc"}
        assert "hub_token" not in state
        assert "hub-token" not in json.dumps(state)

    def test_hub_url_recorded_when_supplied(self, runner: CliRunner, tmp_path: Path) -> None:
        ws = str(tmp_path / "ws")
        ddir = str(tmp_path / "ddir")
        with runner.isolated_filesystem(temp_dir=tmp_path):
            fake_nexusd = "/usr/local/bin/nexusd"
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            with (
                patch("shutil.which", return_value=fake_nexusd),
                patch("subprocess.run", return_value=mock_proc),
                patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True),
            ):
                result = runner.invoke(
                    up,
                    [
                        "--profile",
                        "sandbox",
                        "--workspace",
                        ws,
                        "--data-dir",
                        ddir,
                        "--hub-url",
                        "grpc://hub:50051",
                        "--hub-token",
                        "secret",
                    ],
                )
            assert result.exit_code == 0, result.output
            state = load_runtime_state(ddir)
            assert state["hub_url"] == "grpc://hub:50051"
            assert "secret" not in json.dumps(state)


class TestDeriveGrpcPortHelper:
    """MINOR 6: focused tests for the ONE shared port-derivation helper."""

    def test_explicit_port_plus_two_ignores_env(self) -> None:
        from nexus.cli.state import derive_grpc_port

        with patch.dict("os.environ", {"NEXUS_GRPC_PORT": "9999"}, clear=False):
            # explicit --port wins over inherited NEXUS_GRPC_PORT
            assert derive_grpc_port(4000, port_explicit=True) == 4002

    def test_env_override_when_no_explicit_port(self) -> None:
        from nexus.cli.state import derive_grpc_port

        with patch.dict("os.environ", {"NEXUS_GRPC_PORT": "9999"}, clear=False):
            assert derive_grpc_port(2026, port_explicit=False) == 9999

    def test_default_plus_two(self) -> None:
        from nexus.cli.state import derive_grpc_port

        env = {k: v for k, v in __import__("os").environ.items() if k != "NEXUS_GRPC_PORT"}
        with patch.dict("os.environ", env, clear=True):
            assert derive_grpc_port(2026, port_explicit=False) == 2028


class TestNexusUrlHostConsistency:
    """MINOR 4: NEXUS_URL uses the state-recorded connectable host so HTTP
    and gRPC point at the same place; Docker/legacy state stays
    byte-identical (localhost)."""

    def test_state_host_used_for_nexus_url(self) -> None:
        state = {
            "profile": "sandbox",
            "ports": {"http": 3030, "grpc": 3032},
            "grpc_host": "127.0.0.1",
        }
        env = resolve_connection_env({}, state)
        assert env["NEXUS_URL"] == "http://127.0.0.1:3030"
        assert env["NEXUS_GRPC_HOST"] == "127.0.0.1:3032"

    def test_no_host_key_keeps_localhost(self) -> None:
        # Docker/legacy state has no grpc_host key → unchanged.
        state = {"ports": {"http": 2026, "grpc": 2028}}
        env = resolve_connection_env({}, state)
        assert env["NEXUS_URL"] == "http://localhost:2026"

    def test_wildcard_host_maps_to_localhost_for_url(self) -> None:
        # 0.0.0.0 is a bind wildcard, not connectable → localhost.
        state = {"ports": {"http": 2026, "grpc": 2028}, "grpc_host": "0.0.0.0"}
        env = resolve_connection_env({}, state)
        assert env["NEXUS_URL"] == "http://localhost:2026"


class TestProfileStateOnly:
    """BLOCKER 2: resolve_connection_env must NOT fall back to config for
    NEXUS_PROFILE (state-only, mirrors workspace handling)."""

    def test_config_profile_does_not_leak(self) -> None:
        env = resolve_connection_env({"profile": "shared"}, {})
        assert "NEXUS_PROFILE" not in env

    def test_state_profile_still_emitted(self) -> None:
        env = resolve_connection_env({}, {"profile": "sandbox"})
        assert env["NEXUS_PROFILE"] == "sandbox"
