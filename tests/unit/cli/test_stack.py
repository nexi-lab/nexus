"""Tests for nexus.cli.commands.stack — up/down/logs/restart/upgrade."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from nexus.cli.commands.stack import (
    _compose_profiles,
    _derive_project_env,
    _load_project_config,
    _resolve_image_ref_from_config,
    _resolve_profiles,
    _save_project_config,
    down,
    up,
    upgrade,
)


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def shared_config(tmp_path: Path) -> Path:
    """Write a shared-preset nexus.yaml and return the path."""
    config = {
        "preset": "shared",
        "data_dir": str(tmp_path / "nexus-data"),
        "services": ["nexus", "postgres", "dragonfly", "zoekt"],
        "ports": {"http": 2026, "grpc": 2028, "postgres": 5432, "dragonfly": 6379, "zoekt": 6070},
        "compose_profiles": ["core", "cache", "search"],
        "compose_file": str(tmp_path / "nexus-stack.yml"),
        "auth": "static",
        "tls": False,
        "image_ref": "ghcr.io/nexi-lab/nexus:0.9.2",
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
            "nexus.cli.commands.stack.CONFIG_SEARCH_PATHS",
            (str(shared_config / "nexus.yaml"),),
        ):
            config = _load_project_config()
            assert config["preset"] == "shared"

    def test_load_project_config_missing(self, tmp_path: Path) -> None:
        with (
            patch(
                "nexus.cli.commands.stack.CONFIG_SEARCH_PATHS",
                (str(tmp_path / "nope.yaml"),),
            ),
            pytest.raises(SystemExit),
        ):
            _load_project_config()

    def test_save_project_config(self, shared_config: Path) -> None:
        cfg_path = shared_config / "nexus.yaml"
        with patch(
            "nexus.cli.commands.stack.CONFIG_SEARCH_PATHS",
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
            "nexus.cli.commands.stack.CONFIG_SEARCH_PATHS",
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
            "nexus.cli.commands.stack.CONFIG_SEARCH_PATHS",
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
                "zoekt": 6070,
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
        assert "NEXUS_TLS_ENABLED" not in env

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

    def test_tls_env(self, tmp_path: Path) -> None:
        """When tls is enabled, TLS env vars are set."""
        config = {"data_dir": str(tmp_path / "data"), "tls": True, "ports": {}}
        env = _derive_project_env(config)
        assert env["NEXUS_TLS_ENABLED"] == "true"
        assert env["NEXUS_TLS_CERT"] == "/app/data/tls/server.crt"
        assert env["NEXUS_TLS_KEY"] == "/app/data/tls/server.key"
        assert env["NEXUS_TLS_CA"] == "/app/data/tls/ca.crt"

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
            "nexus.cli.commands.stack.CONFIG_SEARCH_PATHS",
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
            "nexus.cli.commands.stack.CONFIG_SEARCH_PATHS",
            (str(cfg_path),),
        ):
            result = runner.invoke(upgrade)
            assert result.exit_code == 0
            assert "does not use a prebuilt image" in result.output

    def test_already_up_to_date(self, runner: CliRunner, shared_config: Path) -> None:
        with (
            patch(
                "nexus.cli.commands.stack.CONFIG_SEARCH_PATHS",
                (str(shared_config / "nexus.yaml"),),
            ),
            patch(
                "nexus.cli.commands.stack._resolve_image_ref",
                return_value="ghcr.io/nexi-lab/nexus:0.9.2",
            ),
        ):
            result = runner.invoke(upgrade)
            assert result.exit_code == 0
            assert "up to date" in result.output.lower()

    def test_upgrade_with_yes_flag(self, runner: CliRunner, shared_config: Path) -> None:
        with (
            patch(
                "nexus.cli.commands.stack.CONFIG_SEARCH_PATHS",
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
                "nexus.cli.commands.stack.CONFIG_SEARCH_PATHS",
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
