"""Tests for nexus.cli.commands.init_cmd — preset-aware init."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from nexus.cli.commands.init_cmd import (
    PRESET_AUTH,
    PRESET_COMPOSE_PROFILES,
    PRESET_SERVICES,
    VALID_PRESETS,
    _build_config,
    _scaffold_tls,
    init,
)


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def tmp_project(tmp_path: Path) -> Path:
    """Provide a temp directory for init output."""
    return tmp_path


# ---------------------------------------------------------------------------
# Unit tests — _build_config (pure logic, no I/O)
# ---------------------------------------------------------------------------


class TestBuildConfig:
    def test_local_preset(self) -> None:
        cfg = _build_config("local", "./nexus-data", False, {}, ())
        assert cfg["preset"] == "local"
        assert cfg["auth"] == "none"
        assert cfg["tls"] is False
        assert "services" not in cfg
        assert "ports" not in cfg
        assert "compose_profiles" not in cfg

    def test_shared_preset(self) -> None:
        from nexus.cli.port_utils import DEFAULT_PORTS

        cfg = _build_config("shared", "./nexus-data", False, dict(DEFAULT_PORTS), ())
        assert cfg["preset"] == "shared"
        assert cfg["auth"] == "static"
        assert "nexus" in cfg["services"]
        assert "postgres" in cfg["services"]
        assert "http" in cfg["ports"]
        assert cfg["compose_profiles"] == ["core", "cache", "search"]
        assert "compose_file" in cfg

    def test_demo_preset(self) -> None:
        from nexus.cli.port_utils import DEFAULT_PORTS

        cfg = _build_config("demo", "./nexus-data", False, dict(DEFAULT_PORTS), ())
        assert cfg["preset"] == "demo"
        assert cfg["auth"] == "database"
        assert "nexus" in cfg["services"]
        assert "postgres" in cfg["services"]

    def test_tls_flag(self) -> None:
        cfg = _build_config("shared", "./data", True, {}, ())
        assert cfg["tls"] is True
        assert cfg["tls_dir"] == "./data/tls"
        assert cfg["tls_cert"] == "./data/tls/server.crt"
        assert cfg["tls_key"] == "./data/tls/server.key"
        assert cfg["tls_ca"] == "./data/tls/ca.crt"

    def test_addons_included(self) -> None:
        cfg = _build_config("shared", "./data", False, {}, ("nats", "mcp"))
        assert cfg["addons"] == ["nats", "mcp"]

    def test_no_addons_key_when_empty(self) -> None:
        cfg = _build_config("shared", "./data", False, {}, ())
        assert "addons" not in cfg


# ---------------------------------------------------------------------------
# Integration tests — CLI invocation with CliRunner + tmpdir
# ---------------------------------------------------------------------------


class TestInitCliCommand:
    def test_default_local_preset(self, runner: CliRunner, tmp_project: Path) -> None:
        config_path = tmp_project / "nexus.yaml"
        data_dir = tmp_project / "nexus-data"
        result = runner.invoke(
            init,
            ["--config-path", str(config_path), "--data-dir", str(data_dir)],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "local" in result.output
        assert config_path.exists()
        assert data_dir.exists()

        # Verify config contents
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["preset"] == "local"
        assert cfg["auth"] == "none"

    def test_shared_preset(self, runner: CliRunner, tmp_project: Path) -> None:
        config_path = tmp_project / "nexus.yaml"
        data_dir = tmp_project / "nexus-data"
        result = runner.invoke(
            init,
            [
                "--preset",
                "shared",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(data_dir),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "shared" in result.output

        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["preset"] == "shared"
        assert cfg["auth"] == "static"
        assert "postgres" in cfg["services"]

    def test_demo_preset(self, runner: CliRunner, tmp_project: Path) -> None:
        config_path = tmp_project / "nexus.yaml"
        data_dir = tmp_project / "nexus-data"
        result = runner.invoke(
            init,
            [
                "--preset",
                "demo",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(data_dir),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "demo" in result.output
        assert "nexus demo init" in result.output

        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["preset"] == "demo"
        assert cfg["auth"] == "database"

    def test_tls_flag(self, runner: CliRunner, tmp_project: Path) -> None:
        config_path = tmp_project / "nexus.yaml"
        data_dir = tmp_project / "data"
        result = runner.invoke(
            init,
            [
                "--preset",
                "shared",
                "--tls",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(data_dir),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "TLS" in result.output

        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["tls"] is True

        # TLS directory should have been scaffolded
        assert (data_dir / "tls").is_dir()

    def test_refuses_overwrite_without_force(self, runner: CliRunner, tmp_project: Path) -> None:
        config_path = tmp_project / "nexus.yaml"
        config_path.write_text("existing: true\n")

        result = runner.invoke(
            init,
            ["--config-path", str(config_path), "--data-dir", str(tmp_project / "d")],
        )
        assert result.exit_code != 0
        assert "already exists" in result.output

    def test_force_overwrites(self, runner: CliRunner, tmp_project: Path) -> None:
        config_path = tmp_project / "nexus.yaml"
        config_path.write_text("existing: true\n")

        result = runner.invoke(
            init,
            [
                "--force",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(tmp_project / "d"),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["preset"] == "local"

    def test_with_addons(self, runner: CliRunner, tmp_project: Path) -> None:
        config_path = tmp_project / "nexus.yaml"
        result = runner.invoke(
            init,
            [
                "--preset",
                "shared",
                "--with",
                "nats",
                "--with",
                "mcp",
                "--config-path",
                str(config_path),
                "--data-dir",
                str(tmp_project / "d"),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        assert "nats" in cfg["addons"]
        assert "mcp" in cfg["addons"]

    def test_data_dir_created(self, runner: CliRunner, tmp_project: Path) -> None:
        data_dir = tmp_project / "deep" / "nested" / "nexus-data"
        result = runner.invoke(
            init,
            [
                "--config-path",
                str(tmp_project / "nexus.yaml"),
                "--data-dir",
                str(data_dir),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert data_dir.exists()
        assert (data_dir / "cas").exists()


# ---------------------------------------------------------------------------
# Constants tests
# ---------------------------------------------------------------------------


class TestTlsScaffolding:
    def test_creates_tls_dir(self, tmp_path: Path) -> None:
        tls_dir = tmp_path / "tls"
        _scaffold_tls(tls_dir)
        assert tls_dir.is_dir()

    def test_generates_certs_when_openssl_available(self, tmp_path: Path) -> None:
        import shutil

        if not shutil.which("openssl"):
            pytest.skip("openssl not on PATH")
        tls_dir = tmp_path / "tls"
        _scaffold_tls(tls_dir)
        assert (tls_dir / "ca.crt").exists()
        assert (tls_dir / "ca.key").exists()
        assert (tls_dir / "server.crt").exists()
        assert (tls_dir / "server.key").exists()

    def test_idempotent_when_certs_exist(self, tmp_path: Path) -> None:
        tls_dir = tmp_path / "tls"
        tls_dir.mkdir()
        (tls_dir / "ca.crt").write_text("existing")
        (tls_dir / "server.crt").write_text("existing")
        # Should not overwrite
        _scaffold_tls(tls_dir)
        assert (tls_dir / "ca.crt").read_text() == "existing"

    def test_fallback_when_openssl_missing(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        tls_dir = tmp_path / "tls"
        with patch("nexus.cli.commands.init_cmd.shutil.which", return_value=None):
            _scaffold_tls(tls_dir)
        assert tls_dir.is_dir()
        # Certs should NOT exist since openssl is "missing"
        assert not (tls_dir / "server.crt").exists()


class TestPresetConstants:
    def test_valid_presets(self) -> None:
        assert VALID_PRESETS == ("local", "shared", "demo")

    def test_all_presets_have_services(self) -> None:
        for preset in VALID_PRESETS:
            assert preset in PRESET_SERVICES

    def test_all_presets_have_auth(self) -> None:
        for preset in VALID_PRESETS:
            assert preset in PRESET_AUTH

    def test_all_presets_have_compose_profiles(self) -> None:
        for preset in VALID_PRESETS:
            assert preset in PRESET_COMPOSE_PROFILES
