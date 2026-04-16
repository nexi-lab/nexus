"""Tests for CodexSyncAdapter — fixture-based parse + integration tests."""

from __future__ import annotations

import json as _json
import shutil
from pathlib import Path

import pytest

from nexus.bricks.auth.credential_backend import CredentialResolutionError
from nexus.bricks.auth.external_sync.codex_sync import CodexSyncAdapter

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "external_cli_output"
_CREDS_V1 = _FIXTURE_DIR / "codex_credentials_v1.json"
_CREDS_EMPTY = _FIXTURE_DIR / "codex_credentials_empty.json"
_AUTH_CHATGPT = _FIXTURE_DIR / "codex_auth_chatgpt.json"
_AUTH_APIKEY = _FIXTURE_DIR / "codex_auth_apikey.json"


@pytest.fixture()
def adapter() -> CodexSyncAdapter:
    return CodexSyncAdapter()


class TestCodexParse:
    def test_parse_v1_discovers_two_profiles(self, adapter: CodexSyncAdapter) -> None:
        content = _CREDS_V1.read_text(encoding="utf-8")
        profiles = adapter.parse_file(_CREDS_V1, content)

        assert len(profiles) == 2
        names = {p.account_identifier for p in profiles}
        assert "default" in names
        assert "staging" in names

    def test_parse_v1_backend_key_format(self, adapter: CodexSyncAdapter) -> None:
        content = _CREDS_V1.read_text(encoding="utf-8")
        profiles = adapter.parse_file(_CREDS_V1, content)

        for p in profiles:
            assert p.backend_key.startswith("codex/")
            assert p.provider == "codex"
            assert p.source == "codex"

    def test_parse_empty_returns_empty(self, adapter: CodexSyncAdapter) -> None:
        content = _CREDS_EMPTY.read_text(encoding="utf-8")
        profiles = adapter.parse_file(_CREDS_EMPTY, content)
        assert profiles == []

    def test_parse_malformed_raises(self, adapter: CodexSyncAdapter) -> None:
        with pytest.raises(_json.JSONDecodeError):
            adapter.parse_file(Path("bad.json"), "{not json")

    def test_parse_auth_chatgpt_uses_account_id(self, adapter: CodexSyncAdapter) -> None:
        """Current Codex CLI writes auth.json with tokens.account_id."""
        content = _AUTH_CHATGPT.read_text(encoding="utf-8")
        profiles = adapter.parse_file(_AUTH_CHATGPT, content)

        assert len(profiles) == 1
        assert profiles[0].account_identifier == "52d072f2-4fc4-42e3-a870-a79c5d82e1c9"
        assert profiles[0].backend_key.startswith("codex/")

    def test_parse_auth_apikey_uses_auth_mode(self, adapter: CodexSyncAdapter) -> None:
        """API-key mode has OPENAI_API_KEY + auth_mode but no tokens.account_id."""
        content = _AUTH_APIKEY.read_text(encoding="utf-8")
        profiles = adapter.parse_file(_AUTH_APIKEY, content)

        assert len(profiles) == 1
        # Identifier falls through to auth_mode when there's no account_id.
        assert profiles[0].account_identifier == "apikey"


class TestCodexPaths:
    def test_defaults_to_home_codex(
        self, adapter: CodexSyncAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CODEX_CONFIG_DIR", raising=False)
        paths = adapter.paths()
        names = [p.name for p in paths]
        # auth.json preferred (current Codex CLI layout), credentials.json
        # + config.json retained for older installs.
        assert names[0] == "auth.json"
        assert "credentials.json" in names
        assert "config.json" in names

    def test_env_override(self, adapter: CodexSyncAdapter, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CODEX_CONFIG_DIR", "/custom/codex")
        paths = adapter.paths()
        assert paths[0] == Path("/custom/codex/auth.json")


class TestCodexSync:
    async def test_sync_discovers_from_fixture(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cred_file = tmp_path / "credentials.json"
        shutil.copy(_CREDS_V1, cred_file)
        monkeypatch.setenv("CODEX_CONFIG_DIR", str(tmp_path))

        adapter = CodexSyncAdapter()
        result = await adapter.sync()

        assert result.error is None
        assert len(result.profiles) == 2

    async def test_sync_empty_file_returns_no_profiles(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cred_file = tmp_path / "credentials.json"
        shutil.copy(_CREDS_EMPTY, cred_file)
        monkeypatch.setenv("CODEX_CONFIG_DIR", str(tmp_path))

        adapter = CodexSyncAdapter()
        result = await adapter.sync()

        assert result.profiles == []

    async def test_sync_missing_files_returns_degraded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEX_CONFIG_DIR", str(tmp_path / "nope"))
        adapter = CodexSyncAdapter()
        result = await adapter.sync()
        assert result.error is not None

    async def test_detect_true_when_file_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cred_file = tmp_path / "credentials.json"
        shutil.copy(_CREDS_V1, cred_file)
        monkeypatch.setenv("CODEX_CONFIG_DIR", str(tmp_path))
        adapter = CodexSyncAdapter()
        assert await adapter.detect() is True

    async def test_detect_false_when_no_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEX_CONFIG_DIR", str(tmp_path / "nope"))
        adapter = CodexSyncAdapter()
        assert await adapter.detect() is False


class TestCodexResolveCredential:
    async def test_resolve_api_key_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cred_file = tmp_path / "credentials.json"
        shutil.copy(_CREDS_V1, cred_file)
        monkeypatch.setenv("CODEX_CONFIG_DIR", str(tmp_path))

        adapter = CodexSyncAdapter()
        cred = await adapter.resolve_credential("codex/default")
        assert cred.kind == "api_key"
        assert cred.api_key == "sk-codex-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

    async def test_resolve_token_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cred_file = tmp_path / "credentials.json"
        shutil.copy(_CREDS_V1, cred_file)
        monkeypatch.setenv("CODEX_CONFIG_DIR", str(tmp_path))

        adapter = CodexSyncAdapter()
        cred = await adapter.resolve_credential("codex/staging")
        assert cred.kind == "bearer_token"
        assert cred.access_token is not None

    async def test_resolve_missing_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cred_file = tmp_path / "credentials.json"
        shutil.copy(_CREDS_V1, cred_file)
        monkeypatch.setenv("CODEX_CONFIG_DIR", str(tmp_path))

        adapter = CodexSyncAdapter()
        with pytest.raises(CredentialResolutionError, match="nonexistent"):
            await adapter.resolve_credential("codex/nonexistent")

    def test_resolve_sync(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cred_file = tmp_path / "credentials.json"
        shutil.copy(_CREDS_V1, cred_file)
        monkeypatch.setenv("CODEX_CONFIG_DIR", str(tmp_path))

        adapter = CodexSyncAdapter()
        cred = adapter.resolve_credential_sync("codex/default")
        assert cred.kind == "api_key"

    async def test_resolve_auth_chatgpt_returns_access_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Real Codex chatgpt auth.json → bearer_token from tokens.access_token."""
        auth_file = tmp_path / "auth.json"
        shutil.copy(_AUTH_CHATGPT, auth_file)
        monkeypatch.setenv("CODEX_CONFIG_DIR", str(tmp_path))

        adapter = CodexSyncAdapter()
        cred = await adapter.resolve_credential("codex/52d072f2-4fc4-42e3-a870-a79c5d82e1c9")

        assert cred.kind == "bearer_token"
        assert cred.access_token == "eyJhbGciOiJSUzI1NiJ9.test-access-token"

    async def test_resolve_auth_apikey_returns_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        auth_file = tmp_path / "auth.json"
        shutil.copy(_AUTH_APIKEY, auth_file)
        monkeypatch.setenv("CODEX_CONFIG_DIR", str(tmp_path))

        adapter = CodexSyncAdapter()
        cred = await adapter.resolve_credential("codex/apikey")

        assert cred.kind == "api_key"
        assert cred.api_key == "sk-codex-real-api-key-example"
