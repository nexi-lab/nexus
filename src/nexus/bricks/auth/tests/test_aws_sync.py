"""Tests for AwsCliSyncAdapter — fixture-based parse + integration tests."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from nexus.bricks.auth.credential_backend import CredentialResolutionError
from nexus.bricks.auth.external_sync.aws_sync import AwsCliSyncAdapter

# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "external_cli_output"
_V215 = _FIXTURE_DIR / "aws_credentials_v2.15.ini"
_V216 = _FIXTURE_DIR / "aws_credentials_v2.16.ini"


@pytest.fixture()
def adapter() -> AwsCliSyncAdapter:
    return AwsCliSyncAdapter()


# ---------------------------------------------------------------------------
# TestAwsParseCredentials — parse both fixtures
# ---------------------------------------------------------------------------


class TestAwsParseCredentials:
    """Test parse_file against real-format fixture files."""

    def test_parse_v215_discovers_two_profiles(self, adapter: AwsCliSyncAdapter) -> None:
        content = _V215.read_text(encoding="utf-8")
        profiles = adapter.parse_file(_V215, content)

        names = {p.account_identifier for p in profiles}
        assert "default" in names
        assert "work-prod" in names
        assert len(profiles) == 2

    def test_parse_v215_skips_no_key_profile(self, adapter: AwsCliSyncAdapter) -> None:
        content = _V215.read_text(encoding="utf-8")
        profiles = adapter.parse_file(_V215, content)

        names = {p.account_identifier for p in profiles}
        assert "no-key-profile" not in names

    def test_parse_v216_discovers_four_profiles(self, adapter: AwsCliSyncAdapter) -> None:
        content = _V216.read_text(encoding="utf-8")
        profiles = adapter.parse_file(_V216, content)

        names = {p.account_identifier for p in profiles}
        assert "default" in names
        assert "sso-session" in names
        assert "dev" in names
        assert "future-unknown-field-profile" in names
        assert len(profiles) == 4

    def test_parse_v216_tolerates_unknown_keys(self, adapter: AwsCliSyncAdapter) -> None:
        """Profiles with unrecognized fields should still be discovered."""
        content = _V216.read_text(encoding="utf-8")
        profiles = adapter.parse_file(_V216, content)

        future = [p for p in profiles if p.account_identifier == "future-unknown-field-profile"]
        assert len(future) == 1

    def test_parse_empty_file_returns_empty(self, adapter: AwsCliSyncAdapter) -> None:
        profiles = adapter.parse_file(Path("/dev/null"), "")
        assert profiles == []

    def test_backend_key_format(self, adapter: AwsCliSyncAdapter) -> None:
        content = _V215.read_text(encoding="utf-8")
        profiles = adapter.parse_file(_V215, content)

        for p in profiles:
            assert p.backend_key.startswith("aws-cli/")
            assert p.backend_key == f"aws-cli/{p.account_identifier}"

    def test_all_profiles_have_s3_provider(self, adapter: AwsCliSyncAdapter) -> None:
        content = _V215.read_text(encoding="utf-8")
        profiles = adapter.parse_file(_V215, content)

        for p in profiles:
            assert p.provider == "s3"
            assert p.source == "aws-cli"


# ---------------------------------------------------------------------------
# TestAwsParseConfig — "profile <name>" prefix handling
# ---------------------------------------------------------------------------


class TestAwsParseConfig:
    """Test parse_file with ~/.aws/config style 'profile <name>' sections."""

    def test_profile_prefix_stripped(self, adapter: AwsCliSyncAdapter) -> None:
        config_content = """\
[default]
aws_access_key_id = AKIADEFAULTEXAMPLE
aws_secret_access_key = defaultSecretExample

[profile staging]
aws_access_key_id = AKIASTAGINGEXAMPLE
aws_secret_access_key = stagingSecretExample
region = us-east-1

[profile production]
aws_access_key_id = AKIAPRODEXAMPLE
aws_secret_access_key = prodSecretExample
"""
        profiles = adapter.parse_file(Path("~/.aws/config"), config_content)

        names = {p.account_identifier for p in profiles}
        assert names == {"default", "staging", "production"}

    def test_profile_prefix_not_in_backend_key(self, adapter: AwsCliSyncAdapter) -> None:
        config_content = """\
[profile my-team]
aws_access_key_id = AKIATEAMEXAMPLE
aws_secret_access_key = teamSecretExample
"""
        profiles = adapter.parse_file(Path("~/.aws/config"), config_content)

        assert len(profiles) == 1
        assert profiles[0].backend_key == "aws-cli/my-team"
        assert profiles[0].account_identifier == "my-team"


# ---------------------------------------------------------------------------
# TestAwsPaths — env var overrides and defaults
# ---------------------------------------------------------------------------


class TestAwsPaths:
    def test_defaults_to_home_aws(
        self, adapter: AwsCliSyncAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AWS_SHARED_CREDENTIALS_FILE", raising=False)
        monkeypatch.delenv("AWS_CONFIG_FILE", raising=False)

        paths = adapter.paths()
        assert len(paths) == 2
        assert paths[0] == Path("~/.aws/credentials").expanduser()
        assert paths[1] == Path("~/.aws/config").expanduser()

    def test_env_var_override_credentials(
        self, adapter: AwsCliSyncAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/custom/creds")
        monkeypatch.delenv("AWS_CONFIG_FILE", raising=False)

        paths = adapter.paths()
        assert paths[0] == Path("/custom/creds")

    def test_env_var_override_config(
        self, adapter: AwsCliSyncAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AWS_SHARED_CREDENTIALS_FILE", raising=False)
        monkeypatch.setenv("AWS_CONFIG_FILE", "/custom/config")

        paths = adapter.paths()
        assert paths[1] == Path("/custom/config")


# ---------------------------------------------------------------------------
# TestAwsSync — full integration via FileAdapter.sync()
# ---------------------------------------------------------------------------


class TestAwsSync:
    async def test_sync_discovers_profiles_from_fixture(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cred_file = tmp_path / "credentials"
        shutil.copy(_V215, cred_file)

        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(cred_file))
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "nonexistent-config"))

        adapter = AwsCliSyncAdapter()
        result = await adapter.sync()

        assert result.adapter_name == "aws-cli"
        assert result.error is None
        assert len(result.profiles) == 2
        names = {p.account_identifier for p in result.profiles}
        assert names == {"default", "work-prod"}

    async def test_sync_merges_credentials_and_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cred_file = tmp_path / "credentials"
        config_file = tmp_path / "config"

        cred_file.write_text(
            "[default]\naws_access_key_id = AKIACRED\naws_secret_access_key = credSecret\n",
            encoding="utf-8",
        )
        config_file.write_text(
            "[profile extra]\naws_access_key_id = AKIAEXTRA\naws_secret_access_key = extraSecret\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(cred_file))
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))

        adapter = AwsCliSyncAdapter()
        result = await adapter.sync()

        assert result.error is None
        names = {p.account_identifier for p in result.profiles}
        assert names == {"default", "extra"}

    async def test_sync_credentials_file_takes_precedence_over_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When both files have the same profile, credentials wins."""
        cred_file = tmp_path / "credentials"
        config_file = tmp_path / "config"

        # Both files define "default" — credentials should win
        cred_file.write_text(
            "[default]\naws_access_key_id = AKIACRED\naws_secret_access_key = credSecret\n",
            encoding="utf-8",
        )
        config_file.write_text(
            "[default]\naws_access_key_id = AKIACONFIG\naws_secret_access_key = configSecret\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(cred_file))
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))

        adapter = AwsCliSyncAdapter()
        result = await adapter.sync()

        assert result.error is None
        # Only one profile — deduped by backend_key, first file wins
        assert len(result.profiles) == 1
        assert result.profiles[0].account_identifier == "default"

    async def test_sync_missing_files_returns_degraded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "nope-creds"))
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "nope-config"))

        adapter = AwsCliSyncAdapter()
        result = await adapter.sync()

        assert result.error is not None
        assert result.profiles == []

    async def test_detect_true_when_fixture_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cred_file = tmp_path / "credentials"
        shutil.copy(_V215, cred_file)

        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(cred_file))
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "nonexistent"))

        adapter = AwsCliSyncAdapter()
        assert await adapter.detect() is True

    async def test_detect_false_when_no_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "nope-creds"))
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "nope-config"))

        adapter = AwsCliSyncAdapter()
        assert await adapter.detect() is False


# ---------------------------------------------------------------------------
# TestAwsResolveCredential — resolve from fixture file
# ---------------------------------------------------------------------------


class TestAwsResolveCredential:
    async def test_resolve_default_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cred_file = tmp_path / "credentials"
        shutil.copy(_V215, cred_file)

        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(cred_file))
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "nonexistent"))

        adapter = AwsCliSyncAdapter()
        cred = await adapter.resolve_credential("aws-cli/default")

        assert cred.kind == "api_key"
        assert cred.api_key == "AKIAIOSFODNN7EXAMPLE"
        assert cred.metadata["secret_access_key"] == "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"

    async def test_resolve_named_profile_with_region(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cred_file = tmp_path / "credentials"
        shutil.copy(_V215, cred_file)

        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(cred_file))
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "nonexistent"))

        adapter = AwsCliSyncAdapter()
        cred = await adapter.resolve_credential("aws-cli/work-prod")

        assert cred.api_key == "AKIAI44QH8DHBEXAMPLE"
        assert cred.metadata["region"] == "us-west-2"

    async def test_resolve_sso_session_with_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cred_file = tmp_path / "credentials"
        shutil.copy(_V216, cred_file)

        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(cred_file))
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "nonexistent"))

        adapter = AwsCliSyncAdapter()
        cred = await adapter.resolve_credential("aws-cli/sso-session")

        assert cred.api_key == "ASIAZZZZZZZZZEXAMPLE"
        assert cred.metadata["session_token"] == "FwoGZXIvYXdzEBYaDHqa0AP1/EXAMPLETOKEN"
        assert cred.metadata["secret_access_key"] == "tempSecretFromSSO/EXAMPLEKEY"

    async def test_resolve_from_config_profile_prefix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config"
        config_file.write_text(
            "[profile my-team]\n"
            "aws_access_key_id = AKIATEAMKEY\n"
            "aws_secret_access_key = teamSecret\n"
            "region = ap-southeast-1\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "nonexistent"))
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))

        adapter = AwsCliSyncAdapter()
        cred = await adapter.resolve_credential("aws-cli/my-team")

        assert cred.api_key == "AKIATEAMKEY"
        assert cred.metadata["secret_access_key"] == "teamSecret"
        assert cred.metadata["region"] == "ap-southeast-1"

    async def test_resolve_missing_profile_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cred_file = tmp_path / "credentials"
        shutil.copy(_V215, cred_file)

        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(cred_file))
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "nonexistent"))

        adapter = AwsCliSyncAdapter()
        with pytest.raises(CredentialResolutionError, match="nonexistent-profile"):
            await adapter.resolve_credential("aws-cli/nonexistent-profile")

    async def test_resolve_no_files_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "nope"))
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "nope2"))

        adapter = AwsCliSyncAdapter()
        with pytest.raises(CredentialResolutionError):
            await adapter.resolve_credential("aws-cli/default")
