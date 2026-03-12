"""Tests for nexus.cli.commands.stack — up/down/logs/restart."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from nexus.cli.commands.stack import (
    _load_project_config,
    _save_project_config,
    down,
    up,
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
# `nexus down` — behavior tests
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
