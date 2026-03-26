from __future__ import annotations

from pathlib import Path

import pytest

from nexus.bricks.auth.unified_service import FileSecretCredentialStore, UnifiedAuthService
from nexus.contracts.unified_auth import AuthStatus, CredentialKind


class _FakeOAuthService:
    def __init__(self) -> None:
        self._credentials = [
            {
                "provider": "google",
                "user_email": "alice@example.com",
                "is_expired": False,
            }
        ]

    async def list_credentials(self, context=None):  # noqa: ANN001, ARG002
        return list(self._credentials)

    async def test_credential(self, provider: str, user_email: str, context=None):  # noqa: ANN001, ARG002
        return {
            "success": provider == "google" and user_email == "alice@example.com",
            "provider": provider,
            "user_email": user_email,
            "message": "OAuth credential is valid.",
        }


@pytest.fixture
def secret_store(tmp_path: Path) -> FileSecretCredentialStore:
    return FileSecretCredentialStore(tmp_path / "credentials.json")


@pytest.fixture
def auth_service(secret_store: FileSecretCredentialStore) -> UnifiedAuthService:
    return UnifiedAuthService(oauth_service=_FakeOAuthService(), secret_store=secret_store)


def test_connect_secret_persists_record(auth_service: UnifiedAuthService) -> None:
    record = auth_service.connect_secret(
        "s3",
        {
            "access_key_id": "AKIA...",
            "secret_access_key": "secret",
            "region_name": "us-east-1",
        },
    )

    assert record.kind == CredentialKind.SECRET
    assert record.data["access_key_id"] == "AKIA..."


def test_resolve_backend_config_uses_stored_secret(auth_service: UnifiedAuthService) -> None:
    auth_service.connect_secret(
        "s3",
        {
            "access_key_id": "AKIA...",
            "secret_access_key": "secret",
        },
    )

    resolution = auth_service.resolve_backend_config("path_s3", {"bucket": "demo"})

    assert resolution.status == AuthStatus.AUTHED
    assert resolution.source == "stored:secret"
    assert resolution.resolved_config["access_key_id"] == "AKIA..."


def test_list_summaries_includes_oauth_and_secret(auth_service: UnifiedAuthService) -> None:
    import asyncio

    auth_service.connect_secret(
        "gcs",
        {
            "credentials_path": "/tmp/gcs.json",
        },
    )

    summaries = asyncio.run(auth_service.list_summaries())

    summary_by_service = {summary.service: summary for summary in summaries}
    assert summary_by_service["gcs"].status == AuthStatus.AUTHED
    assert summary_by_service["google-drive"].status == AuthStatus.AUTHED
    assert summary_by_service["gws"].status == AuthStatus.AUTHED


def test_test_service_reports_missing_secret(
    auth_service: UnifiedAuthService, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    monkeypatch.setattr(auth_service, "_detect_native", lambda service: None)
    result = asyncio.run(auth_service.test_service("s3"))

    assert result["success"] is False
    assert "nexus auth connect s3" in result["message"]


def test_file_store_delete(secret_store: FileSecretCredentialStore) -> None:
    secret_store.upsert("s3", CredentialKind.NATIVE, {})

    assert secret_store.delete("s3") is True
    assert secret_store.get("s3") is None


def test_test_service_supports_gws_alias(auth_service: UnifiedAuthService) -> None:
    import asyncio

    result = asyncio.run(auth_service.test_service("gws", user_email="alice@example.com"))

    assert result["success"] is True
    assert result["service"] == "gws"
    assert result["provider"] == "google"


def test_list_summaries_prefers_native_gws_when_stored_oauth_expired(
    secret_store: FileSecretCredentialStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    oauth = _FakeOAuthService()
    oauth._credentials = [
        {
            "provider": "google",
            "user_email": "alice@example.com",
            "is_expired": True,
        }
    ]
    service = UnifiedAuthService(oauth_service=oauth, secret_store=secret_store)
    monkeypatch.setattr(
        service,
        "_detect_google_workspace_cli_native",
        lambda user_email=None: {
            "source": "native:gws_cli",
            "email": "alice@example.com",
            "message": "Local gws CLI profile available for alice@example.com.",
        },
    )

    summaries = asyncio.run(service.list_summaries())
    summary_by_service = {summary.service: summary for summary in summaries}

    assert summary_by_service["gws"].status == AuthStatus.AUTHED
    assert summary_by_service["gws"].kind == CredentialKind.NATIVE
    assert summary_by_service["gws"].source == "native:gws_cli"
    assert summary_by_service["gws"].details["stored_oauth_status"] == AuthStatus.EXPIRED.value


def test_test_service_prefers_native_gws_when_stored_oauth_expired(
    secret_store: FileSecretCredentialStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    oauth = _FakeOAuthService()
    oauth._credentials = [
        {
            "provider": "google",
            "user_email": "alice@example.com",
            "is_expired": True,
        }
    ]
    service = UnifiedAuthService(oauth_service=oauth, secret_store=secret_store)
    monkeypatch.setattr(
        service,
        "_detect_google_workspace_cli_native",
        lambda user_email=None: {
            "source": "native:gws_cli",
            "email": "alice@example.com",
            "message": "Local gws CLI profile available for alice@example.com.",
        },
    )

    result = asyncio.run(service.test_service("gws", user_email="alice@example.com"))

    assert result["success"] is True
    assert result["source"] == "native:gws_cli"
    assert "expired" in result["message"].lower()
