"""Tests for GhCliSyncAdapter — fixture-based parse + integration tests."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.bricks.auth.credential_backend import CredentialResolutionError
from nexus.bricks.auth.external_sync.gh_sync import GhCliSyncAdapter

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "external_cli_output"
_HOSTS_V240 = _FIXTURE_DIR / "gh_hosts_v2.40.yml"
_HOSTS_V250 = _FIXTURE_DIR / "gh_hosts_v2.50.yml"
_STATUS_V240 = _FIXTURE_DIR / "gh_auth_status_v2.40.txt"
_STATUS_V250 = _FIXTURE_DIR / "gh_auth_status_v2.50.txt"


@pytest.fixture()
def adapter() -> GhCliSyncAdapter:
    return GhCliSyncAdapter()


class TestGhParseHosts:
    """Test _parse_hosts_file against both hosts.yml formats."""

    def test_parse_v240_flat_format(self, adapter: GhCliSyncAdapter) -> None:
        content = _HOSTS_V240.read_text(encoding="utf-8")
        profiles = adapter.parse_hosts_file(content)

        assert len(profiles) == 1
        assert profiles[0].account_identifier == "testuser"
        assert profiles[0].backend_key == "gh-cli/github.com/testuser"
        assert profiles[0].provider == "github"
        assert profiles[0].source == "gh-cli"

    def test_parse_v250_nested_format(self, adapter: GhCliSyncAdapter) -> None:
        content = _HOSTS_V250.read_text(encoding="utf-8")
        profiles = adapter.parse_hosts_file(content)

        names = {p.account_identifier for p in profiles}
        assert "testuser" in names
        assert "workuser" in names
        assert "corpuser" in names
        assert len(profiles) == 3

    def test_parse_v250_enterprise_host(self, adapter: GhCliSyncAdapter) -> None:
        content = _HOSTS_V250.read_text(encoding="utf-8")
        profiles = adapter.parse_hosts_file(content)

        corp = [p for p in profiles if p.account_identifier == "corpuser"]
        assert len(corp) == 1
        assert corp[0].backend_key == "gh-cli/enterprise.corp.com/corpuser"

    def test_parse_empty_returns_empty(self, adapter: GhCliSyncAdapter) -> None:
        profiles = adapter.parse_hosts_file("")
        assert profiles == []


class TestGhParseAuthStatus:
    """Test _parse_status_output against gh auth status --show-token output."""

    def test_parse_v240_single_host(self, adapter: GhCliSyncAdapter) -> None:
        content = _STATUS_V240.read_text(encoding="utf-8")
        profiles = adapter.parse_status_output(content)

        assert len(profiles) == 1
        assert profiles[0].account_identifier == "testuser"
        assert profiles[0].backend_key == "gh-cli/github.com/testuser"

    def test_parse_v250_multiple_hosts(self, adapter: GhCliSyncAdapter) -> None:
        content = _STATUS_V250.read_text(encoding="utf-8")
        profiles = adapter.parse_status_output(content)

        assert len(profiles) == 2
        names = {p.account_identifier for p in profiles}
        assert "testuser" in names
        assert "corpuser" in names


class TestGhPaths:
    def test_default_config_dir(
        self, adapter: GhCliSyncAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
        config_dir = adapter._config_dir()
        assert str(config_dir).endswith(".config/gh")

    def test_gh_config_dir_override(
        self, adapter: GhCliSyncAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GH_CONFIG_DIR", "/custom/gh")
        assert adapter._config_dir() == Path("/custom/gh")


class TestGhSync:
    async def test_sync_file_fallback_discovers_profiles(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When gh binary is missing, falls back to hosts.yml."""
        config_dir = tmp_path / "gh"
        config_dir.mkdir()
        shutil.copy(_HOSTS_V250, config_dir / "hosts.yml")
        monkeypatch.setenv("GH_CONFIG_DIR", str(config_dir))

        with patch("nexus.bricks.auth.external_sync.gh_sync.shutil.which", return_value=None):
            adapter = GhCliSyncAdapter()
            result = await adapter.sync()

        assert result.error is None
        assert len(result.profiles) == 3

    async def test_sync_missing_binary_and_file_returns_degraded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "nope"))
        with patch("nexus.bricks.auth.external_sync.gh_sync.shutil.which", return_value=None):
            adapter = GhCliSyncAdapter()
            result = await adapter.sync()
        assert result.error is not None
        assert result.profiles == []

    async def test_detect_true_with_binary(self) -> None:
        with patch(
            "nexus.bricks.auth.external_sync.gh_sync.shutil.which",
            return_value="/usr/bin/gh",
        ):
            adapter = GhCliSyncAdapter()
            assert await adapter.detect() is True

    async def test_detect_true_with_hosts_file_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / "gh"
        config_dir.mkdir()
        shutil.copy(_HOSTS_V240, config_dir / "hosts.yml")
        monkeypatch.setenv("GH_CONFIG_DIR", str(config_dir))

        with patch("nexus.bricks.auth.external_sync.gh_sync.shutil.which", return_value=None):
            adapter = GhCliSyncAdapter()
            assert await adapter.detect() is True

    async def test_detect_false_nothing_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "nope"))
        with patch("nexus.bricks.auth.external_sync.gh_sync.shutil.which", return_value=None):
            adapter = GhCliSyncAdapter()
            assert await adapter.detect() is False


class TestGhResolveCredential:
    async def test_resolve_from_hosts_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / "gh"
        config_dir.mkdir()
        shutil.copy(_HOSTS_V250, config_dir / "hosts.yml")
        monkeypatch.setenv("GH_CONFIG_DIR", str(config_dir))

        adapter = GhCliSyncAdapter()
        cred = await adapter.resolve_credential("gh-cli/github.com/testuser")

        assert cred.kind == "bearer_token"
        assert cred.access_token == "gho_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx50"

    async def test_resolve_missing_user_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / "gh"
        config_dir.mkdir()
        shutil.copy(_HOSTS_V250, config_dir / "hosts.yml")
        monkeypatch.setenv("GH_CONFIG_DIR", str(config_dir))

        adapter = GhCliSyncAdapter()
        with pytest.raises(CredentialResolutionError):
            await adapter.resolve_credential("gh-cli/github.com/nobody")

    def test_resolve_sync_from_hosts_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / "gh"
        config_dir.mkdir()
        shutil.copy(_HOSTS_V240, config_dir / "hosts.yml")
        monkeypatch.setenv("GH_CONFIG_DIR", str(config_dir))

        adapter = GhCliSyncAdapter()
        cred = adapter.resolve_credential_sync("gh-cli/github.com/testuser")

        assert cred.access_token == "gho_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx40"
