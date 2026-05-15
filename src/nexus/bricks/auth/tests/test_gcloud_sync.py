"""Tests for GcloudSyncAdapter — fixture-based parse + integration tests."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from nexus.bricks.auth.credential_backend import CredentialResolutionError
from nexus.bricks.auth.external_sync.gcloud_sync import GcloudSyncAdapter

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "external_cli_output"
_ADC_V456 = _FIXTURE_DIR / "gcloud_adc_v456.json"
_ADC_SA = _FIXTURE_DIR / "gcloud_adc_service_account.json"
_PROPS_V456 = _FIXTURE_DIR / "gcloud_properties_v456.ini"
_CONFIG_DEFAULT = _FIXTURE_DIR / "gcloud_config_default.ini"


@pytest.fixture()
def adapter() -> GcloudSyncAdapter:
    return GcloudSyncAdapter()


class TestGcloudParseAdc:
    def test_parse_authorized_user_returns_empty(self, adapter: GcloudSyncAdapter) -> None:
        """ADC authorized_user carries no email — account discovery comes from properties."""
        content = _ADC_V456.read_text(encoding="utf-8")
        profiles = adapter.parse_file(_ADC_V456, content)
        assert profiles == []

    def test_parse_service_account_extracts_email(self, adapter: GcloudSyncAdapter) -> None:
        content = _ADC_SA.read_text(encoding="utf-8")
        profiles = adapter.parse_file(_ADC_SA, content)
        assert len(profiles) == 1
        assert profiles[0].account_identifier == "my-sa@my-project-456.iam.gserviceaccount.com"
        assert profiles[0].backend_key == "gcloud/my-sa@my-project-456.iam.gserviceaccount.com"
        assert profiles[0].provider == "gcs"

    def test_parse_empty_returns_empty(self, adapter: GcloudSyncAdapter) -> None:
        profiles = adapter.parse_file(Path("/dev/null"), "")
        assert profiles == []

    def test_parse_malformed_json_raises(self, adapter: GcloudSyncAdapter) -> None:
        import json as _json

        with pytest.raises(_json.JSONDecodeError):
            adapter.parse_file(Path("bad.json"), "{not valid json at all")


class TestGcloudParseProperties:
    def test_parse_properties_extracts_account(self, adapter: GcloudSyncAdapter) -> None:
        content = _PROPS_V456.read_text(encoding="utf-8")
        profiles = adapter.parse_file(_PROPS_V456, content)
        assert len(profiles) == 1
        assert profiles[0].account_identifier == "user@example.com"
        assert profiles[0].backend_key == "gcloud/user@example.com"
        assert profiles[0].provider == "gcs"

    def test_parse_properties_no_account_returns_empty(self, adapter: GcloudSyncAdapter) -> None:
        profiles = adapter.parse_file(Path("p.ini"), "[compute]\nregion = us-central1\n")
        assert profiles == []


class TestGcloudPaths:
    def test_defaults_to_home_gcloud(
        self, adapter: GcloudSyncAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLOUDSDK_CONFIG", raising=False)
        paths = adapter.paths()
        # ADC first, then active_config's per-configuration file (if present),
        # then legacy flat properties. The exact count depends on whether the
        # host has active_config — we only assert the anchors.
        assert "application_default_credentials.json" in str(paths[0])
        assert any("properties" in str(p) or "configurations" in str(p) for p in paths[1:])

    def test_cloudsdk_config_override(
        self, adapter: GcloudSyncAdapter, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))
        paths = adapter.paths()
        assert paths[0] == tmp_path / "application_default_credentials.json"
        assert paths[-1] == tmp_path / "properties"


class TestGcloudSync:
    async def test_sync_discovers_service_account(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adc = tmp_path / "application_default_credentials.json"
        props = tmp_path / "properties"
        shutil.copy(_ADC_SA, adc)
        shutil.copy(_PROPS_V456, props)
        monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))
        adapter = GcloudSyncAdapter()
        result = await adapter.sync()
        assert result.error is None
        assert len(result.profiles) >= 1
        emails = {p.account_identifier for p in result.profiles}
        assert "my-sa@my-project-456.iam.gserviceaccount.com" in emails

    async def test_sync_authorized_user_with_properties_stitches_account(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADC authorized_user alone is not enough; properties file supplies the email.

        Verifies the fix for the pre-merge review finding C4 — ADC must not
        emit a phantom ``gcloud/unknown`` profile. The account comes entirely
        from the properties file.
        """
        adc = tmp_path / "application_default_credentials.json"
        props = tmp_path / "properties"
        shutil.copy(_ADC_V456, adc)  # authorized_user
        shutil.copy(_PROPS_V456, props)  # [core] account=user@example.com
        monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))

        adapter = GcloudSyncAdapter()
        result = await adapter.sync()

        assert result.error is None
        assert len(result.profiles) == 1
        profile = result.profiles[0]
        assert profile.account_identifier == "user@example.com"
        assert profile.backend_key == "gcloud/user@example.com"
        # No phantom profile
        assert not any(p.account_identifier == "unknown" for p in result.profiles)

    async def test_sync_reads_active_configuration_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Real gcloud uses ``configurations/config_<name>`` plus ``active_config``.

        Regression test: the adapter must read the active configuration's
        properties file, not just the legacy flat ``properties``.
        """
        adc = tmp_path / "application_default_credentials.json"
        shutil.copy(_ADC_V456, adc)

        configs = tmp_path / "configurations"
        configs.mkdir()
        shutil.copy(_CONFIG_DEFAULT, configs / "config_default")
        (tmp_path / "active_config").write_text("default", encoding="utf-8")
        monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))

        adapter = GcloudSyncAdapter()
        result = await adapter.sync()

        assert result.error is None
        emails = {p.account_identifier for p in result.profiles}
        assert "real.user@example.com" in emails

    async def test_sync_authorized_user_without_properties_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADC authorized_user with no properties file → degraded (no profiles)."""
        adc = tmp_path / "application_default_credentials.json"
        shutil.copy(_ADC_V456, adc)
        monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))

        adapter = GcloudSyncAdapter()
        result = await adapter.sync()

        # No properties file → no email → no profile at all.
        assert result.profiles == []

    async def test_sync_deduplicates_by_backend_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adc = tmp_path / "application_default_credentials.json"
        props = tmp_path / "properties"
        shutil.copy(_ADC_SA, adc)
        shutil.copy(_PROPS_V456, props)
        monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))
        adapter = GcloudSyncAdapter()
        result = await adapter.sync()
        keys = [p.backend_key for p in result.profiles]
        assert len(keys) == len(set(keys)), "Duplicate backend_key found"

    async def test_sync_missing_files_returns_degraded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path / "nonexistent"))
        adapter = GcloudSyncAdapter()
        result = await adapter.sync()
        assert result.error is not None
        assert result.profiles == []

    async def test_detect_true_when_adc_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adc = tmp_path / "application_default_credentials.json"
        shutil.copy(_ADC_V456, adc)
        monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))
        adapter = GcloudSyncAdapter()
        assert await adapter.detect() is True

    async def test_detect_false_when_no_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path / "nope"))
        adapter = GcloudSyncAdapter()
        assert await adapter.detect() is False


class TestGcloudResolveCredential:
    async def test_resolve_service_account(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adc = tmp_path / "application_default_credentials.json"
        shutil.copy(_ADC_SA, adc)
        monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))
        adapter = GcloudSyncAdapter()
        cred = await adapter.resolve_credential(
            "gcloud/my-sa@my-project-456.iam.gserviceaccount.com"
        )
        assert cred.kind == "api_key"
        assert "BEGIN RSA PRIVATE KEY" in (cred.api_key or "")
        assert cred.metadata["client_email"] == "my-sa@my-project-456.iam.gserviceaccount.com"

    async def test_resolve_authorized_user(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adc = tmp_path / "application_default_credentials.json"
        shutil.copy(_ADC_V456, adc)
        monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))
        adapter = GcloudSyncAdapter()
        cred = await adapter.resolve_credential("gcloud/user@example.com")
        assert cred.kind == "bearer_token"
        assert cred.access_token is None
        assert "refresh_token" in cred.metadata

    async def test_resolve_missing_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path / "nope"))
        adapter = GcloudSyncAdapter()
        with pytest.raises(CredentialResolutionError):
            await adapter.resolve_credential("gcloud/nobody@example.com")

    def test_resolve_sync_service_account(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adc = tmp_path / "application_default_credentials.json"
        shutil.copy(_ADC_SA, adc)
        monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))
        adapter = GcloudSyncAdapter()
        cred = adapter.resolve_credential_sync(
            "gcloud/my-sa@my-project-456.iam.gserviceaccount.com"
        )
        assert cred.kind == "api_key"
