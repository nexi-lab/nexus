"""Tests for nexus config show/get/set/reset commands."""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from nexus.cli.commands.config_cmd import config_group
from tests.unit.cli.conftest import make_config


class TestConfigShow:
    def test_show_defaults(self, cli_runner: CliRunner) -> None:
        config = make_config()
        with patch("nexus.cli.commands.config_cmd.load_cli_config", return_value=config):
            result = cli_runner.invoke(config_group, ["show"])
        assert result.exit_code == 0
        assert "output.format" in result.output

    def test_show_json(self, cli_runner: CliRunner) -> None:
        config = make_config()
        with patch("nexus.cli.commands.config_cmd.load_cli_config", return_value=config):
            result = cli_runner.invoke(config_group, ["show", "--json"])
        assert result.exit_code == 0
        assert "output.format" in result.output


class TestConfigGet:
    def test_get_known_key(self, cli_runner: CliRunner) -> None:
        config = make_config(settings={"output": {"format": "json"}})
        with patch("nexus.cli.commands.config_cmd.load_cli_config", return_value=config):
            result = cli_runner.invoke(config_group, ["get", "output.format"])
        assert result.exit_code == 0
        assert "json" in result.output

    def test_get_unknown_key(self, cli_runner: CliRunner) -> None:
        config = make_config()
        with patch("nexus.cli.commands.config_cmd.load_cli_config", return_value=config):
            result = cli_runner.invoke(config_group, ["get", "nonexistent.key"])
        assert result.exit_code == 1
        assert "Unknown" in result.output


class TestConfigSet:
    def test_set_value(self, cli_runner: CliRunner) -> None:
        config = make_config()
        with (
            patch("nexus.cli.commands.config_cmd.load_cli_config", return_value=config),
            patch("nexus.cli.commands.config_cmd.save_cli_config") as mock_save,
        ):
            result = cli_runner.invoke(config_group, ["set", "output.format", "json"])
        assert result.exit_code == 0
        saved = mock_save.call_args[0][0]
        assert saved.settings["output"]["format"] == "json"

    def test_set_boolean(self, cli_runner: CliRunner) -> None:
        config = make_config()
        with (
            patch("nexus.cli.commands.config_cmd.load_cli_config", return_value=config),
            patch("nexus.cli.commands.config_cmd.save_cli_config") as mock_save,
        ):
            result = cli_runner.invoke(config_group, ["set", "timing.enabled", "true"])
        assert result.exit_code == 0
        saved = mock_save.call_args[0][0]
        assert saved.settings["timing"]["enabled"] is True

    def test_set_unknown_key(self, cli_runner: CliRunner) -> None:
        config = make_config()
        with patch("nexus.cli.commands.config_cmd.load_cli_config", return_value=config):
            result = cli_runner.invoke(config_group, ["set", "bad.key", "val"])
        assert result.exit_code == 1


class TestConfigReset:
    def test_reset_to_default(self, cli_runner: CliRunner) -> None:
        config = make_config(settings={"output": {"format": "json"}})
        with (
            patch("nexus.cli.commands.config_cmd.load_cli_config", return_value=config),
            patch("nexus.cli.commands.config_cmd.save_cli_config") as mock_save,
        ):
            result = cli_runner.invoke(config_group, ["reset", "output.format"])
        assert result.exit_code == 0
        saved = mock_save.call_args[0][0]
        assert saved.settings["output"]["format"] == "table"

    def test_reset_unknown_key(self, cli_runner: CliRunner) -> None:
        config = make_config()
        with patch("nexus.cli.commands.config_cmd.load_cli_config", return_value=config):
            result = cli_runner.invoke(config_group, ["reset", "bad.key"])
        assert result.exit_code == 1
