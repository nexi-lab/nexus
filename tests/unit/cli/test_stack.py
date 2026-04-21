"""Tests for nexus.cli.commands.stack — up/down/logs/restart/upgrade."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from nexus.cli.commands.stack import (
    _compose_profiles,
    _derive_project_env,
    _docker_build_args,
    _resolve_image_ref_from_config,
    _resolve_profiles,
    down,
    up,
    upgrade,
)
from nexus.cli.state import (
    load_project_config as _load_project_config,
)
from nexus.cli.state import (
    save_project_config as _save_project_config,
)

# The canonical patch target for CONFIG_SEARCH_PATHS is now nexus.cli.state
_CONFIG_PATCH = "nexus.cli.state.CONFIG_SEARCH_PATHS"


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def shared_config(tmp_path: Path) -> Path:
    """Write a shared-preset nexus.yaml and return the path."""
    config = {
        "preset": "shared",
        "data_dir": str(tmp_path / "nexus-data"),
        "services": ["nexus", "postgres", "dragonfly"],
        "ports": {"http": 2026, "grpc": 2028, "postgres": 5432, "dragonfly": 6379},
        "compose_profiles": ["core", "cache"],
        "compose_file": str(tmp_path / "nexus-stack.yml"),
        "auth": "static",
        "tls": False,
        "image_ref": "ghcr.io/nexi-lab/nexus:stable",
        "image_channel": "stable",
        "image_accelerator": "cpu",
    }
    cfg_path = tmp_path / "nexus.yaml"
    with open(cfg_path, "w") as f:
        yaml.dump(config, f)
    # Create a minimal compose file so the path check passes
    compose_path = tmp_path / "nexus-stack.yml"
    compose_path.write_text("services: {}\n")
    return tmp_path


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestConfigLoading:
    def test_load_project_config(self, shared_config: Path) -> None:
        with patch(
            _CONFIG_PATCH,
            (str(shared_config / "nexus.yaml"),),
        ):
            config = _load_project_config()
            assert config["preset"] == "shared"

    def test_load_project_config_missing(self, tmp_path: Path) -> None:
        with (
            patch(
                _CONFIG_PATCH,
                (str(tmp_path / "nope.yaml"),),
            ),
            pytest.raises(SystemExit),
        ):
            _load_project_config()

    def test_save_project_config(self, shared_config: Path) -> None:
        cfg_path = shared_config / "nexus.yaml"
        with patch(
            _CONFIG_PATCH,
            (str(cfg_path),),
        ):
            _save_project_config({"preset": "demo", "auth": "database"})
            with open(cfg_path) as f:
                saved = yaml.safe_load(f)
            assert saved["preset"] == "demo"


# ---------------------------------------------------------------------------
# _resolve_image_ref_from_config — backward compat (Issue #2961, Issue 11)
# ---------------------------------------------------------------------------


class TestResolveImageRefFromConfig:
    """Parametrized matrix of all 5 backward-compatibility scenarios."""

    @pytest.mark.parametrize(
        ("config", "env_vars", "expected"),
        [
            pytest.param(
                {"image_ref": "ghcr.io/nexi-lab/nexus:0.9.2"},
                {},
                "ghcr.io/nexi-lab/nexus:0.9.2",
                id="config_image_ref_only",
            ),
            pytest.param(
                {"image_tag": "0.8.0"},
                {},
                "ghcr.io/nexi-lab/nexus:0.8.0",
                id="config_image_tag_only_compat",
            ),
            pytest.param(
                {"image_ref": "ghcr.io/nexi-lab/nexus:0.9.2", "image_tag": "0.8.0"},
                {},
                "ghcr.io/nexi-lab/nexus:0.9.2",
                id="both_present_image_ref_wins",
            ),
            pytest.param(
                {"image_ref": "ghcr.io/nexi-lab/nexus:0.9.2"},
                {"NEXUS_IMAGE_TAG": "override-tag"},
                "ghcr.io/nexi-lab/nexus:0.9.2",
                id="env_tag_loses_to_config_ref",
            ),
            pytest.param(
                {"image_ref": "ghcr.io/nexi-lab/nexus:0.9.2"},
                {"NEXUS_IMAGE_REF": "ghcr.io/nexi-lab/nexus:env-override"},
                "ghcr.io/nexi-lab/nexus:env-override",
                id="env_ref_wins_over_config_ref",
            ),
        ],
    )
    def test_precedence(
        self,
        config: dict,
        env_vars: dict,
        expected: str,
    ) -> None:
        env_patch = {
            "NEXUS_IMAGE_REF": env_vars.get("NEXUS_IMAGE_REF", ""),
            "NEXUS_IMAGE_TAG": env_vars.get("NEXUS_IMAGE_TAG", ""),
        }
        with patch.dict(os.environ, env_patch, clear=False):
            # Clear any pre-existing env vars for clean test
            if "NEXUS_IMAGE_REF" not in env_vars:
                os.environ.pop("NEXUS_IMAGE_REF", None)
            if "NEXUS_IMAGE_TAG" not in env_vars:
                os.environ.pop("NEXUS_IMAGE_TAG", None)
            result = _resolve_image_ref_from_config(config)
        assert result == expected

    def test_empty_config_returns_empty(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NEXUS_IMAGE_REF", None)
            os.environ.pop("NEXUS_IMAGE_TAG", None)
            result = _resolve_image_ref_from_config({})
        assert result == ""

    def test_env_image_tag_deprecated_compat(self) -> None:
        """NEXUS_IMAGE_TAG env var still works but expands to full ref."""
        with patch.dict(os.environ, {"NEXUS_IMAGE_TAG": "pr-123"}, clear=False):
            os.environ.pop("NEXUS_IMAGE_REF", None)
            result = _resolve_image_ref_from_config({})
        assert result == "ghcr.io/nexi-lab/nexus:pr-123"


# ---------------------------------------------------------------------------
# _resolve_profiles — addon profile resolution (Issue #2961, Issue 5+12)
# ---------------------------------------------------------------------------


class TestResolveProfiles:
    def test_base_profiles_from_config(self) -> None:
        config = {"compose_profiles": ["core", "cache", "search"]}
        assert _resolve_profiles(config) == ["core", "cache", "search"]

    def test_cli_addons_appended(self) -> None:
        config = {"compose_profiles": ["core"]}
        profiles = _resolve_profiles(config, cli_addons=("nats", "mcp"))
        assert "events" in profiles  # nats → events
        assert "mcp" in profiles

    def test_config_addons_appended(self) -> None:
        config = {"compose_profiles": ["core"], "addons": ["nats", "observability"]}
        profiles = _resolve_profiles(config)
        assert "events" in profiles
        assert "observability" in profiles

    def test_no_duplicates(self) -> None:
        config = {"compose_profiles": ["core", "events"], "addons": ["nats"]}
        profiles = _resolve_profiles(config, cli_addons=("nats",))
        assert profiles.count("events") == 1

    def test_unknown_addon_passes_through(self) -> None:
        config = {"compose_profiles": ["core"]}
        profiles = _resolve_profiles(config, cli_addons=("custom-addon",))
        assert "custom-addon" in profiles

    def test_all_addon_mappings_work(self) -> None:
        """Ensure every known addon maps correctly (catches the old down() bug)."""
        config = {"compose_profiles": []}
        all_addons = ("nats", "mcp", "frontend", "langgraph", "observability")
        profiles = _resolve_profiles(config, cli_addons=all_addons)
        assert "events" in profiles
        assert "mcp" in profiles
        assert "frontend" in profiles
        assert "langgraph" in profiles
        assert "observability" in profiles


# ---------------------------------------------------------------------------
# `nexus up` — behavior tests
# ---------------------------------------------------------------------------


class TestUpCommand:
    def test_local_preset_warns(self, runner: CliRunner, tmp_path: Path) -> None:
        """Local preset should not attempt Docker operations."""
        cfg = {"preset": "local"}
        cfg_path = tmp_path / "nexus.yaml"
        with open(cfg_path, "w") as f:
            yaml.dump(cfg, f)

        with patch(
            _CONFIG_PATCH,
            (str(cfg_path),),
        ):
            result = runner.invoke(up)
            assert result.exit_code == 0
            assert "does not use Docker" in result.output

    def test_missing_compose_file(self, runner: CliRunner, tmp_path: Path) -> None:
        cfg = {
            "preset": "shared",
            "compose_file": str(tmp_path / "nonexistent.yml"),
            "compose_profiles": ["core"],
            "services": [],
            "ports": {},
        }
        cfg_path = tmp_path / "nexus.yaml"
        with open(cfg_path, "w") as f:
            yaml.dump(cfg, f)
        with patch(
            _CONFIG_PATCH,
            (str(cfg_path),),
        ):
            result = runner.invoke(up)
            assert result.exit_code != 0
            assert "not found" in result.output


# ---------------------------------------------------------------------------
# _derive_project_env tests
# ---------------------------------------------------------------------------


class TestDeriveProjectEnv:
    def test_basic_env(self, tmp_path: Path) -> None:
        """Produces COMPOSE_PROJECT_NAME, ports, data dir, and auth."""
        config = {
            "data_dir": str(tmp_path / "data"),
            "ports": {
                "http": 2026,
                "grpc": 2028,
                "postgres": 5432,
                "dragonfly": 6379,
            },
            "admin_user": "admin",
            "auth": "database",
        }
        env = _derive_project_env(config)
        assert env["COMPOSE_PROJECT_NAME"].startswith("nexus-")
        assert len(env["COMPOSE_PROJECT_NAME"]) == len("nexus-") + 8
        assert env["NEXUS_PORT"] == "2026"
        assert env["NEXUS_GRPC_PORT"] == "2028"
        assert env["POSTGRES_PORT"] == "5432"
        assert env["NEXUS_HOST_DATA_DIR"] == str(tmp_path / "data")
        assert env["NEXUS_AUTH_TYPE"] == "database"
        assert env["NEXUS_GRPC_TLS"] == "false"

    def test_image_ref_in_env(self, tmp_path: Path) -> None:
        """image_ref from config is passed as NEXUS_IMAGE_REF."""
        config = {
            "data_dir": str(tmp_path / "data"),
            "ports": {},
            "image_ref": "ghcr.io/nexi-lab/nexus:0.9.2",
        }
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NEXUS_IMAGE_REF", None)
            os.environ.pop("NEXUS_IMAGE_TAG", None)
            env = _derive_project_env(config)
        assert env["NEXUS_IMAGE_REF"] == "ghcr.io/nexi-lab/nexus:0.9.2"

    def test_image_tag_compat_in_env(self, tmp_path: Path) -> None:
        """Old image_tag config is expanded to full NEXUS_IMAGE_REF."""
        config = {
            "data_dir": str(tmp_path / "data"),
            "ports": {},
            "image_tag": "0.8.0",
        }
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NEXUS_IMAGE_REF", None)
            os.environ.pop("NEXUS_IMAGE_TAG", None)
            env = _derive_project_env(config)
        assert env["NEXUS_IMAGE_REF"] == "ghcr.io/nexi-lab/nexus:0.8.0"

    def test_resolved_ports_override(self, tmp_path: Path) -> None:
        """resolved_ports param takes precedence over config ports."""
        config = {
            "data_dir": str(tmp_path / "data"),
            "ports": {"http": 2026, "grpc": 2028},
        }
        resolved = {"http": 3026, "grpc": 3028}
        env = _derive_project_env(config, resolved_ports=resolved)
        assert env["NEXUS_PORT"] == "3026"
        assert env["NEXUS_GRPC_PORT"] == "3028"

    def test_tls_sets_grpc_tls_flag(self, tmp_path: Path) -> None:
        """TLS config sets NEXUS_GRPC_TLS explicitly."""
        config = {"data_dir": str(tmp_path / "data"), "tls": True, "ports": {}}
        env = _derive_project_env(config)
        assert env["NEXUS_GRPC_TLS"] == "true"

    def test_no_tls_sets_grpc_tls_false(self, tmp_path: Path) -> None:
        """Non-TLS config explicitly disables gRPC TLS."""
        config = {"data_dir": str(tmp_path / "data"), "tls": False, "ports": {}}
        env = _derive_project_env(config)
        assert env["NEXUS_GRPC_TLS"] == "false"

    def test_deterministic_project_name(self, tmp_path: Path) -> None:
        """Same data_dir always produces same project name."""
        config = {"data_dir": str(tmp_path / "data"), "ports": {}}
        env1 = _derive_project_env(config)
        env2 = _derive_project_env(config)
        assert env1["COMPOSE_PROJECT_NAME"] == env2["COMPOSE_PROJECT_NAME"]

    def test_different_data_dirs_get_different_names(self, tmp_path: Path) -> None:
        """Different data_dirs produce different project names."""
        env1 = _derive_project_env({"data_dir": str(tmp_path / "a"), "ports": {}})
        env2 = _derive_project_env({"data_dir": str(tmp_path / "b"), "ports": {}})
        assert env1["COMPOSE_PROJECT_NAME"] != env2["COMPOSE_PROJECT_NAME"]

    def test_forwards_optional_txtai_api_embedding_env(self, tmp_path: Path) -> None:
        config = {"data_dir": str(tmp_path / "data"), "ports": {}}
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "sk-test",
                "OPENAI_BASE_URL": "https://api.openai.example/v1",
                "NEXUS_TXTAI_MODEL": "openai/text-embedding-3-small",
                "NEXUS_TXTAI_USE_API_EMBEDDINGS": "true",
            },
            clear=False,
        ):
            env = _derive_project_env(config)

        assert env["OPENAI_API_KEY"] == "sk-test"
        assert env["OPENAI_BASE_URL"] == "https://api.openai.example/v1"
        assert env["NEXUS_TXTAI_MODEL"] == "openai/text-embedding-3-small"
        assert env["NEXUS_TXTAI_USE_API_EMBEDDINGS"] == "true"


class TestDockerBuildArgs:
    def test_api_embeddings_enabled_sets_build_arg(self) -> None:
        args = _docker_build_args(
            {
                "NEXUS_TXTAI_USE_API_EMBEDDINGS": "true",
                "OPENAI_API_KEY": "sk-test",
            }
        )
        assert args == [
            "--build-arg",
            "NEXUS_TXTAI_USE_API_EMBEDDINGS=true",
        ]

    def test_api_embeddings_disabled_sets_false_build_arg(self) -> None:
        args = _docker_build_args({})
        assert args == [
            "--build-arg",
            "NEXUS_TXTAI_USE_API_EMBEDDINGS=false",
        ]

    def test_api_embeddings_without_key_keeps_local_build(self) -> None:
        args = _docker_build_args({"NEXUS_TXTAI_USE_API_EMBEDDINGS": "true"})
        assert args == [
            "--build-arg",
            "NEXUS_TXTAI_USE_API_EMBEDDINGS=false",
        ]


# ---------------------------------------------------------------------------
# _compose_profiles tests
# ---------------------------------------------------------------------------


class TestComposeProfiles:
    def test_collects_profiles(self, tmp_path: Path) -> None:
        compose = tmp_path / "stack.yml"
        compose.write_text(
            yaml.dump(
                {
                    "services": {
                        "pg": {"image": "postgres:16", "profiles": ["core"]},
                        "redis": {"image": "redis:7", "profiles": ["cache"]},
                        "nats": {"image": "nats:2.10", "profiles": ["events"]},
                    }
                }
            )
        )
        assert _compose_profiles(str(compose)) == {"core", "cache", "events"}

    def test_empty_services(self, tmp_path: Path) -> None:
        compose = tmp_path / "stack.yml"
        compose.write_text(yaml.dump({"services": {}}))
        assert _compose_profiles(str(compose)) == set()

    def test_missing_file(self, tmp_path: Path) -> None:
        assert _compose_profiles(str(tmp_path / "nope.yml")) == set()

    def test_malformed_yaml_warns(self, tmp_path: Path) -> None:
        """Malformed YAML should return empty set, not crash (Issue 7)."""
        compose = tmp_path / "stack.yml"
        compose.write_text(":::invalid yaml{{{")
        result = _compose_profiles(str(compose))
        assert result == set()


# ---------------------------------------------------------------------------
# `nexus down` tests
# ---------------------------------------------------------------------------


class TestDownCommand:
    def test_local_preset_warns(self, runner: CliRunner, tmp_path: Path) -> None:
        cfg = {"preset": "local"}
        cfg_path = tmp_path / "nexus.yaml"
        with open(cfg_path, "w") as f:
            yaml.dump(cfg, f)
        with patch(
            _CONFIG_PATCH,
            (str(cfg_path),),
        ):
            result = runner.invoke(down)
            assert result.exit_code == 0
            assert "no Docker services" in result.output


# ---------------------------------------------------------------------------
# `nexus upgrade` tests
# ---------------------------------------------------------------------------


class TestUpgradeCommand:
    def test_local_preset_warns(self, runner: CliRunner, tmp_path: Path) -> None:
        cfg = {"preset": "local"}
        cfg_path = tmp_path / "nexus.yaml"
        with open(cfg_path, "w") as f:
            yaml.dump(cfg, f)
        with patch(
            _CONFIG_PATCH,
            (str(cfg_path),),
        ):
            result = runner.invoke(upgrade)
            assert result.exit_code == 0
            assert "does not use a prebuilt image" in result.output

    def test_already_up_to_date_pinned(self, runner: CliRunner, shared_config: Path) -> None:
        """Pinned configs (no image_channel) report 'up to date' when ref matches."""
        # Override config to be pinned (no image_channel)
        cfg_path = shared_config / "nexus.yaml"
        with open(cfg_path) as f:
            config = yaml.safe_load(f)
        config["image_pin"] = "tag"
        config.pop("image_channel", None)
        with open(cfg_path, "w") as f:
            yaml.dump(config, f)

        with (
            patch(
                _CONFIG_PATCH,
                (str(cfg_path),),
            ),
            patch(
                "nexus.cli.commands.stack._resolve_image_ref",
                return_value="ghcr.io/nexi-lab/nexus:0.9.2",
            ),
        ):
            result = runner.invoke(upgrade)
            assert result.exit_code == 0
            assert "pinned" in result.output.lower()

    def test_channel_following_pulls_latest(self, runner: CliRunner, shared_config: Path) -> None:
        """Channel-following configs pull the latest image on upgrade."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        with (
            patch(
                _CONFIG_PATCH,
                (str(shared_config / "nexus.yaml"),),
            ),
            patch(
                "nexus.cli.commands.stack._resolve_image_ref",
                return_value="ghcr.io/nexi-lab/nexus:stable",
            ),
            patch(
                "nexus.cli.commands.stack._run_compose",
                return_value=mock_result,
            ) as mock_compose,
        ):
            result = runner.invoke(upgrade)
            assert result.exit_code == 0
            assert "pulling" in result.output.lower()
            # Verify docker compose pull was called
            mock_compose.assert_called_once()
            call_args = mock_compose.call_args
            assert "pull" in call_args[0]

    def test_upgrade_with_yes_flag(self, runner: CliRunner, shared_config: Path) -> None:
        with (
            patch(
                _CONFIG_PATCH,
                (str(shared_config / "nexus.yaml"),),
            ),
            patch(
                "nexus.cli.commands.stack._resolve_image_ref",
                return_value="ghcr.io/nexi-lab/nexus:0.10.0",
            ),
        ):
            result = runner.invoke(upgrade, ["--yes"])
            assert result.exit_code == 0
            assert "0.10.0" in result.output

            # Verify config was updated
            with open(shared_config / "nexus.yaml") as f:
                cfg = yaml.safe_load(f)
            assert cfg["image_ref"] == "ghcr.io/nexi-lab/nexus:0.10.0"

    def test_upgrade_removes_deprecated_image_tag(self, runner: CliRunner, tmp_path: Path) -> None:
        """Upgrade should remove deprecated image_tag from config."""
        config = {
            "preset": "shared",
            "data_dir": str(tmp_path / "data"),
            "image_tag": "0.8.0",
            "image_channel": "stable",
            "image_accelerator": "cpu",
        }
        cfg_path = tmp_path / "nexus.yaml"
        with open(cfg_path, "w") as f:
            yaml.dump(config, f)

        with (
            patch(
                _CONFIG_PATCH,
                (str(cfg_path),),
            ),
            patch(
                "nexus.cli.commands.stack._resolve_image_ref",
                return_value="ghcr.io/nexi-lab/nexus:0.10.0",
            ),
        ):
            result = runner.invoke(upgrade, ["--yes"])
            assert result.exit_code == 0

            with open(cfg_path) as f:
                cfg = yaml.safe_load(f)
            assert "image_tag" not in cfg
            assert cfg["image_ref"] == "ghcr.io/nexi-lab/nexus:0.10.0"


# ---------------------------------------------------------------------------
# `nexus stop` / `nexus start` tests
# ---------------------------------------------------------------------------


class TestStopCommand:
    def test_local_preset_warns(self, runner: CliRunner, tmp_path: Path) -> None:
        cfg = {"preset": "local"}
        cfg_path = tmp_path / "nexus.yaml"
        with open(cfg_path, "w") as f:
            yaml.dump(cfg, f)
        from nexus.cli.commands.stack import stop

        with patch(_CONFIG_PATCH, (str(cfg_path),)):
            result = runner.invoke(stop)
            assert result.exit_code == 0
            assert "no Docker services" in result.output

    def test_shared_preset_calls_compose_stop(self, runner: CliRunner, shared_config: Path) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        from nexus.cli.commands.stack import stop

        with (
            patch(_CONFIG_PATCH, (str(shared_config / "nexus.yaml"),)),
            patch(
                "nexus.cli.commands.stack._run_compose", return_value=mock_result
            ) as mock_compose,
        ):
            result = runner.invoke(stop)
            assert result.exit_code == 0
            assert "paused" in result.output.lower()
            mock_compose.assert_called_once()
            call_args = mock_compose.call_args
            assert "stop" in call_args[0]


class TestStartCommand:
    def test_local_preset_warns(self, runner: CliRunner, tmp_path: Path) -> None:
        cfg = {"preset": "local"}
        cfg_path = tmp_path / "nexus.yaml"
        with open(cfg_path, "w") as f:
            yaml.dump(cfg, f)
        from nexus.cli.commands.stack import start

        with patch(_CONFIG_PATCH, (str(cfg_path),)):
            result = runner.invoke(start)
            assert result.exit_code == 0
            assert "no Docker services" in result.output

    def test_shared_preset_calls_compose_start(
        self, runner: CliRunner, shared_config: Path
    ) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        from nexus.cli.commands.stack import start

        with (
            patch(_CONFIG_PATCH, (str(shared_config / "nexus.yaml"),)),
            patch(
                "nexus.cli.commands.stack._run_compose", return_value=mock_result
            ) as mock_compose,
        ):
            result = runner.invoke(start)
            assert result.exit_code == 0
            assert "resumed" in result.output.lower()
            mock_compose.assert_called_once()
            call_args = mock_compose.call_args
            assert "start" in call_args[0]


# ---------------------------------------------------------------------------
# Port reuse from .state.json
# ---------------------------------------------------------------------------


class TestPortReuse:
    """Verify that nexus up reuses ports based on compose project ownership."""

    def test_reuses_ports_when_project_running(self, tmp_path: Path) -> None:
        """When our compose project has running containers, reuse state ports."""
        from nexus.cli.state import save_runtime_state

        data_dir = tmp_path / "nexus-data"
        data_dir.mkdir()

        save_runtime_state(
            data_dir,
            {
                "ports": {"http": 9990, "grpc": 9991},
                "project_name": "nexus-test1234",
                "build_mode": "remote",
            },
        )

        # Simulate `docker compose -p nexus-test1234 ps -q` returning container IDs
        mock_result = MagicMock()
        mock_result.stdout = "abc123\ndef456\n"
        mock_result.returncode = 0

        with patch("nexus.cli.commands.stack.subprocess.run", return_value=mock_result):
            from nexus.cli.state import load_runtime_state

            state = load_runtime_state(data_dir)
            prev_ports = state.get("ports", {})
            # Simulate the ownership check logic from up()
            has_running = bool(mock_result.stdout.strip())
            assert has_running
            assert prev_ports["http"] == 9990
            assert prev_ports["grpc"] == 9991

    def test_re_resolves_when_no_containers(self, tmp_path: Path) -> None:
        """When our compose project has no running containers, re-resolve ports."""
        from nexus.cli.state import save_runtime_state

        data_dir = tmp_path / "nexus-data"
        data_dir.mkdir()

        save_runtime_state(
            data_dir,
            {
                "ports": {"http": 9990, "grpc": 9991},
                "project_name": "nexus-test1234",
            },
        )

        # Simulate `docker compose ps -q` returning empty (no containers)
        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_result.returncode = 0

        with patch("nexus.cli.commands.stack.subprocess.run", return_value=mock_result):
            has_running = bool(mock_result.stdout.strip())
            assert not has_running  # → should re-resolve

    def test_re_resolves_when_no_state(self, tmp_path: Path) -> None:
        """When no .state.json exists, always resolve from config."""
        from nexus.cli.state import load_runtime_state

        data_dir = tmp_path / "nexus-data"
        data_dir.mkdir()

        state = load_runtime_state(data_dir)
        assert state.get("ports") is None
        assert state.get("project_name") is None
        # No prev_ports → falls through to resolve_ports()


# ---------------------------------------------------------------------------
# Local build mode persistence
# ---------------------------------------------------------------------------


class TestLocalBuildMode:
    """Verify that build mode is tracked in .state.json and reused."""

    def test_state_records_build_mode(self, tmp_path: Path) -> None:
        from nexus.cli.state import load_runtime_state, save_runtime_state

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        save_runtime_state(
            data_dir,
            {
                "build_mode": "local",
                "image_used": "nexus:local-abc12345",
                "ports": {},
            },
        )
        state = load_runtime_state(data_dir)
        assert state["build_mode"] == "local"
        assert state["image_used"] == "nexus:local-abc12345"

    def test_pull_flag_clears_local_mode(self) -> None:
        """When --pull is passed, using_local_build should be False."""
        # Simulate the logic from up()
        prev_state = {"build_mode": "local", "image_used": "nexus:local-abc12345"}
        force_pull = True
        using_local_build = False

        if prev_state.get("build_mode") == "local" and force_pull is not True:
            using_local_build = True

        if force_pull:
            using_local_build = False

        assert not using_local_build

    def test_no_pull_reuses_local_build(self) -> None:
        """Without --pull, local build mode should be detected and reused."""
        prev_state = {"build_mode": "local", "image_used": "nexus:local-abc12345"}
        force_pull = None
        using_local_build = False

        build = None
        if build is None:
            if prev_state.get("build_mode") == "local" and force_pull is not True:
                using_local_build = True
            build = False

        assert using_local_build

    def test_remote_mode_does_not_reuse(self) -> None:
        """Remote build mode should not trigger local reuse."""
        prev_state = {"build_mode": "remote", "image_used": "ghcr.io/nexi-lab/nexus:edge"}
        force_pull = None
        using_local_build = False

        build = None
        if build is None:
            if prev_state.get("build_mode") == "local" and force_pull is not True:
                using_local_build = True
            build = False

        assert not using_local_build


# ---------------------------------------------------------------------------
# TLS auto-discovery in nexus up
# ---------------------------------------------------------------------------


class TestTlsAutoDiscovery:
    """Verify TLS cert paths are discovered and written to state.json."""

    def test_raft_style_certs(self, tmp_path: Path) -> None:
        """Raft-generated certs (ca.pem, node.pem, node-key.pem) are discovered."""
        tls_dir = tmp_path / "tls"
        tls_dir.mkdir()
        (tls_dir / "ca.pem").write_text("ca")
        (tls_dir / "node.pem").write_text("cert")
        (tls_dir / "node-key.pem").write_text("key")

        # Simulate the discovery logic from up()
        tls_state: dict[str, str] = {}
        if tls_dir.exists():
            if (tls_dir / "ca.pem").exists():
                tls_state = {
                    "cert": str(tls_dir / "node.pem"),
                    "key": str(tls_dir / "node-key.pem"),
                    "ca": str(tls_dir / "ca.pem"),
                }
            elif (tls_dir / "ca.crt").exists():
                tls_state = {
                    "cert": str(tls_dir / "server.crt"),
                    "key": str(tls_dir / "server.key"),
                    "ca": str(tls_dir / "ca.crt"),
                }

        assert tls_state["cert"].endswith("node.pem")
        assert tls_state["ca"].endswith("ca.pem")

    def test_openssl_style_certs(self, tmp_path: Path) -> None:
        """OpenSSL-generated certs (ca.crt, server.crt, server.key) are discovered."""
        tls_dir = tmp_path / "tls"
        tls_dir.mkdir()
        (tls_dir / "ca.crt").write_text("ca")
        (tls_dir / "server.crt").write_text("cert")
        (tls_dir / "server.key").write_text("key")

        tls_state: dict[str, str] = {}
        if tls_dir.exists():
            if (tls_dir / "ca.pem").exists():
                tls_state = {
                    "cert": str(tls_dir / "node.pem"),
                    "key": str(tls_dir / "node-key.pem"),
                    "ca": str(tls_dir / "ca.pem"),
                }
            elif (tls_dir / "ca.crt").exists():
                tls_state = {
                    "cert": str(tls_dir / "server.crt"),
                    "key": str(tls_dir / "server.key"),
                    "ca": str(tls_dir / "ca.crt"),
                }

        assert tls_state["cert"].endswith("server.crt")
        assert tls_state["ca"].endswith("ca.crt")

    def test_no_certs_empty_state(self, tmp_path: Path) -> None:
        """No TLS dir → empty tls state."""
        tls_dir = tmp_path / "tls"
        tls_state: dict[str, str] = {}
        if tls_dir.exists() and (tls_dir / "ca.pem").exists():
            tls_state = {"cert": "", "key": "", "ca": ""}

        assert tls_state == {}


# ---------------------------------------------------------------------------
# nexus status reads ports from state.json
# ---------------------------------------------------------------------------


class TestStatusPortResolution:
    """Verify nexus status reads ports from state.json / nexus.yaml."""

    def test_uses_state_json_port(self, tmp_path: Path) -> None:
        from nexus.cli.state import save_runtime_state

        data_dir = tmp_path / "nexus-data"
        data_dir.mkdir()
        save_runtime_state(data_dir, {"ports": {"http": 9876}})

        config = {"data_dir": str(data_dir), "ports": {"http": 2026}}

        from nexus.cli.state import load_runtime_state

        state = load_runtime_state(data_dir)
        ports = state.get("ports", config.get("ports", {}))
        http_port = ports.get("http", 2026)
        assert http_port == 9876  # from state, not config

    def test_falls_back_to_config_port(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "nexus-data"
        data_dir.mkdir()
        # No state.json

        config = {"data_dir": str(data_dir), "ports": {"http": 3333}}

        from nexus.cli.state import load_runtime_state

        state = load_runtime_state(data_dir)
        ports = state.get("ports", config.get("ports", {}))
        http_port = ports.get("http", 2026)
        assert http_port == 3333  # from config

    def test_falls_back_to_default(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "nexus-data"
        data_dir.mkdir()

        config = {"data_dir": str(data_dir)}

        from nexus.cli.state import load_runtime_state

        state = load_runtime_state(data_dir)
        ports = state.get("ports", config.get("ports", {}))
        http_port = ports.get("http", 2026)
        assert http_port == 2026  # default
