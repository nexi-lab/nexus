"""Tests for nexus profile commands."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from nexus.cli.commands.profile import profile_group
from nexus.cli.config import NexusCliConfig
from tests.unit.cli.conftest import make_config

# ---------------------------------------------------------------------------
# profile list
# ---------------------------------------------------------------------------


class TestProfileList:
    def test_no_profiles(self, cli_runner: CliRunner, tmp_config_file: Path) -> None:
        with patch("nexus.cli.commands.profile.load_cli_config") as mock_load:
            mock_load.return_value = NexusCliConfig()
            result = cli_runner.invoke(profile_group, ["list"])
        assert result.exit_code == 0
        assert "No profiles" in result.output

    def test_lists_profiles_with_active_marker(self, cli_runner: CliRunner) -> None:
        config = make_config(
            current_profile="prod",
            profiles={
                "prod": {"url": "http://prod", "api_key": "nx_live_prod_abc123"},
                "local": {"url": "http://localhost:2026"},
            },
        )
        with patch("nexus.cli.commands.profile.load_cli_config", return_value=config):
            result = cli_runner.invoke(profile_group, ["list"])
        assert result.exit_code == 0
        assert "prod" in result.output
        assert "local" in result.output

    def test_api_key_masked(self, cli_runner: CliRunner) -> None:
        config = make_config(
            profiles={"test": {"url": "http://x", "api_key": "nx_live_supersecretkey123"}},
        )
        with patch("nexus.cli.commands.profile.load_cli_config", return_value=config):
            result = cli_runner.invoke(profile_group, ["list"])
        assert result.exit_code == 0
        # Full key should NOT appear in output
        assert "nx_live_supersecretkey123" not in result.output
        # But partial should
        assert "nx_live_" in result.output


# ---------------------------------------------------------------------------
# profile use
# ---------------------------------------------------------------------------


class TestProfileUse:
    def test_switch_profile(self, cli_runner: CliRunner) -> None:
        config = make_config(
            current_profile="local",
            profiles={
                "local": {"url": "http://localhost:2026"},
                "staging": {"url": "http://staging"},
            },
        )
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch("nexus.cli.commands.profile.save_cli_config") as mock_save,
        ):
            result = cli_runner.invoke(profile_group, ["use", "staging"])
        assert result.exit_code == 0
        assert "staging" in result.output
        saved_config = mock_save.call_args[0][0]
        assert saved_config.current_profile == "staging"

    def test_use_nonexistent_profile(self, cli_runner: CliRunner) -> None:
        config = make_config(profiles={"real": {"url": "http://real"}})
        with patch("nexus.cli.commands.profile.load_cli_config", return_value=config):
            result = cli_runner.invoke(profile_group, ["use", "fake"])
        assert result.exit_code == 1
        assert "not found" in result.output


# ---------------------------------------------------------------------------
# profile add
# ---------------------------------------------------------------------------


class TestProfileAdd:
    def test_add_new_profile(self, cli_runner: CliRunner) -> None:
        config = make_config()
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch("nexus.cli.commands.profile.save_cli_config") as mock_save,
        ):
            result = cli_runner.invoke(
                profile_group,
                ["add", "staging", "--url", "http://staging", "--api-key", "key1"],
            )
        assert result.exit_code == 0
        assert "staging" in result.output
        saved = mock_save.call_args[0][0]
        assert "staging" in saved.profiles
        assert saved.profiles["staging"].url == "http://staging"
        assert saved.profiles["staging"].api_key == "key1"

    def test_add_with_use_flag(self, cli_runner: CliRunner) -> None:
        config = make_config()
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch("nexus.cli.commands.profile.save_cli_config") as mock_save,
        ):
            result = cli_runner.invoke(
                profile_group,
                ["add", "new-env", "--url", "http://new", "--use"],
            )
        assert result.exit_code == 0
        saved = mock_save.call_args[0][0]
        assert saved.current_profile == "new-env"

    def test_add_duplicate_fails(self, cli_runner: CliRunner) -> None:
        config = make_config(profiles={"existing": {"url": "http://x"}})
        with patch("nexus.cli.commands.profile.load_cli_config", return_value=config):
            result = cli_runner.invoke(
                profile_group,
                ["add", "existing", "--url", "http://y"],
            )
        assert result.exit_code == 1
        assert "already exists" in result.output


# ---------------------------------------------------------------------------
# profile delete
# ---------------------------------------------------------------------------


class TestProfileDelete:
    def test_delete_profile(self, cli_runner: CliRunner) -> None:
        config = make_config(
            current_profile="other",
            profiles={"victim": {"url": "http://x"}, "other": {"url": "http://y"}},
        )
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch("nexus.cli.commands.profile.save_cli_config") as mock_save,
        ):
            result = cli_runner.invoke(profile_group, ["delete", "victim", "--force"])
        assert result.exit_code == 0
        saved = mock_save.call_args[0][0]
        assert "victim" not in saved.profiles

    def test_delete_active_profile_clears_current(self, cli_runner: CliRunner) -> None:
        config = make_config(
            current_profile="active",
            profiles={"active": {"url": "http://x"}},
        )
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch("nexus.cli.commands.profile.save_cli_config") as mock_save,
        ):
            result = cli_runner.invoke(profile_group, ["delete", "active", "--force"])
        assert result.exit_code == 0
        saved = mock_save.call_args[0][0]
        assert saved.current_profile is None

    def test_delete_nonexistent(self, cli_runner: CliRunner) -> None:
        config = make_config()
        with patch("nexus.cli.commands.profile.load_cli_config", return_value=config):
            result = cli_runner.invoke(profile_group, ["delete", "nope", "--force"])
        assert result.exit_code == 1
        assert "not found" in result.output


# ---------------------------------------------------------------------------
# profile show
# ---------------------------------------------------------------------------


class TestProfileShow:
    def test_show_active_profile(self, cli_runner: CliRunner) -> None:
        config = make_config(
            current_profile="prod",
            profiles={
                "prod": {"url": "http://prod", "api_key": "nx_live_abc", "zone_id": "us-west-1"}
            },
        )
        with patch("nexus.cli.commands.profile.load_cli_config", return_value=config):
            result = cli_runner.invoke(profile_group, ["show"])
        assert result.exit_code == 0
        assert "prod" in result.output
        assert "http://prod" in result.output

    def test_show_no_active(self, cli_runner: CliRunner) -> None:
        config = make_config()
        with patch("nexus.cli.commands.profile.load_cli_config", return_value=config):
            result = cli_runner.invoke(profile_group, ["show"])
        assert result.exit_code == 0
        assert "No active profile" in result.output or "local" in result.output


# ---------------------------------------------------------------------------
# profile rename
# ---------------------------------------------------------------------------


class TestProfileRename:
    def test_rename_profile(self, cli_runner: CliRunner) -> None:
        config = make_config(
            current_profile="old",
            profiles={"old": {"url": "http://x"}},
        )
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch("nexus.cli.commands.profile.save_cli_config") as mock_save,
        ):
            result = cli_runner.invoke(profile_group, ["rename", "old", "new"])
        assert result.exit_code == 0
        saved = mock_save.call_args[0][0]
        assert "new" in saved.profiles
        assert "old" not in saved.profiles
        assert saved.current_profile == "new"

    def test_rename_nonexistent(self, cli_runner: CliRunner) -> None:
        config = make_config()
        with patch("nexus.cli.commands.profile.load_cli_config", return_value=config):
            result = cli_runner.invoke(profile_group, ["rename", "nope", "new"])
        assert result.exit_code == 1

    def test_rename_to_existing(self, cli_runner: CliRunner) -> None:
        config = make_config(
            profiles={"a": {"url": "http://a"}, "b": {"url": "http://b"}},
        )
        with patch("nexus.cli.commands.profile.load_cli_config", return_value=config):
            result = cli_runner.invoke(profile_group, ["rename", "a", "b"])
        assert result.exit_code == 1
        assert "already exists" in result.output
