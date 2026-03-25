"""Tests for YAML config loader (Phase 2, Issue #3148).

Tests cover:
- Loading configs from YAML files
- Loading all configs from a directory
- Creating connectors from configs
- Error handling for invalid/missing files
"""

import pytest

from nexus.backends.connectors.cli.config import CLIConnectorConfig
from nexus.backends.connectors.cli.loader import (
    load_all_configs,
    load_connector_config,
)

# ---------------------------------------------------------------------------
# load_connector_config
# ---------------------------------------------------------------------------


class TestLoadConnectorConfig:
    def test_load_valid_yaml(self, tmp_path) -> None:
        config_file = tmp_path / "gmail.yaml"
        config_file.write_text(
            """\
connector:
  cli: gws
  service: gmail
  auth:
    provider: google
  write:
    - path: "SENT/_new.yaml"
      operation: send_email
      schema_ref: nexus.connectors.gmail.SendEmail
      command: "+send"
      traits:
        reversibility: none
        confirm: user
"""
        )
        config = load_connector_config(config_file)
        assert isinstance(config, CLIConnectorConfig)
        assert config.cli == "gws"
        assert config.service == "gmail"
        assert len(config.write) == 1
        assert config.write[0].operation == "send_email"

    def test_load_root_level_config(self, tmp_path) -> None:
        """Config without 'connector:' wrapper key."""
        config_file = tmp_path / "gh.yaml"
        config_file.write_text(
            """\
cli: gh
service: issue
auth:
  provider: github
"""
        )
        config = load_connector_config(config_file)
        assert config.cli == "gh"
        assert config.service == "issue"

    def test_missing_file_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_connector_config("/nonexistent/path/config.yaml")

    def test_invalid_yaml_raises(self, tmp_path) -> None:
        import yaml as _yaml

        config_file = tmp_path / "bad.yaml"
        config_file.write_text("[{not valid yaml: {{{")

        with pytest.raises(_yaml.YAMLError):
            load_connector_config(config_file)

    def test_invalid_config_raises(self, tmp_path) -> None:
        from pydantic import ValidationError

        config_file = tmp_path / "incomplete.yaml"
        config_file.write_text("cli: gws\n")  # Missing service and auth

        with pytest.raises(ValidationError):
            load_connector_config(config_file)

    def test_non_mapping_raises(self, tmp_path) -> None:
        config_file = tmp_path / "list.yaml"
        config_file.write_text("- item1\n- item2\n")

        with pytest.raises(ValueError, match="Expected YAML mapping"):
            load_connector_config(config_file)


# ---------------------------------------------------------------------------
# load_all_configs
# ---------------------------------------------------------------------------


class TestLoadAllConfigs:
    def test_load_directory(self, tmp_path) -> None:
        (tmp_path / "gmail.yaml").write_text(
            "cli: gws\nservice: gmail\nauth:\n  provider: google\n"
        )
        (tmp_path / "github.yml").write_text("cli: gh\nservice: issue\nauth:\n  provider: github\n")

        configs = load_all_configs(tmp_path)
        assert "gmail" in configs
        assert "github" in configs
        assert configs["gmail"].cli == "gws"
        assert configs["github"].cli == "gh"

    def test_nonexistent_directory(self, tmp_path) -> None:
        configs = load_all_configs(tmp_path / "nonexistent")
        assert configs == {}

    def test_skips_invalid_files(self, tmp_path) -> None:
        (tmp_path / "valid.yaml").write_text(
            "cli: gws\nservice: gmail\nauth:\n  provider: google\n"
        )
        (tmp_path / "invalid.yaml").write_text("not: a: valid: connector config\n")

        configs = load_all_configs(tmp_path)
        assert "valid" in configs
        assert "invalid" not in configs

    def test_empty_directory(self, tmp_path) -> None:
        configs = load_all_configs(tmp_path)
        assert configs == {}

    def test_ignores_non_yaml_files(self, tmp_path) -> None:
        (tmp_path / "readme.txt").write_text("This is not a config")
        (tmp_path / "config.json").write_text('{"not": "yaml"}')

        configs = load_all_configs(tmp_path)
        assert configs == {}
