"""Regression tests for stored OAuth candidate selection in UnifiedAuthService."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nexus.bricks.auth.unified_service import UnifiedAuthService
from nexus.contracts.unified_auth import AuthStatus


@pytest.mark.asyncio
async def test_list_summaries_tries_later_working_google_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oauth_service = AsyncMock()
    oauth_service.list_credentials.return_value = [
        {"provider": "google", "user_email": "first@example.com", "is_expired": False},
        {"provider": "google", "user_email": "second@example.com", "is_expired": False},
    ]
    service = UnifiedAuthService(oauth_service=oauth_service)
    monkeypatch.setattr(service, "_gws_native_from_profile_store", lambda: None)

    calls: list[tuple[str, str]] = []

    async def fake_summary(service_name: str, provider: str, user_email: str) -> dict[str, object]:
        calls.append((service_name, user_email))
        if user_email == "first@example.com":
            return {
                "status": AuthStatus.ERROR,
                "message": f"{provider}:{user_email} missing scopes",
                "details": {},
            }
        return {
            "status": AuthStatus.AUTHED,
            "message": f"{provider}:{user_email} ready",
            "details": {},
        }

    monkeypatch.setattr(service, "_google_target_summary_for_stored_oauth", fake_summary)

    summaries = await service.list_summaries()
    gmail = next(summary for summary in summaries if summary.service == "gmail")

    gmail_calls = [user_email for service_name, user_email in calls if service_name == "gmail"]
    assert gmail_calls == ["first@example.com", "second@example.com"]
    assert gmail.status == AuthStatus.AUTHED
    assert "second@example.com" in gmail.message


@pytest.mark.asyncio
async def test_test_service_uses_later_working_google_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oauth_service = AsyncMock()
    oauth_service.list_credentials.return_value = [
        {"provider": "google", "user_email": "first@example.com", "is_expired": False},
        {"provider": "google", "user_email": "second@example.com", "is_expired": False},
    ]
    oauth_service.test_credential = AsyncMock(
        return_value={"valid": True, "source": "oauth", "message": "credential loaded"}
    )
    service = UnifiedAuthService(oauth_service=oauth_service)
    monkeypatch.setattr(service, "_oauth_native_from_profile_store", lambda *_args, **_kwargs: None)

    calls: list[str] = []

    async def fake_target_result(
        service_name: str,
        targets: tuple[str, ...],
        *,
        provider: str,
        user_email: str,
        default_source: str,
    ) -> dict[str, object]:
        calls.append(user_email)
        if user_email == "first@example.com":
            return {
                "success": False,
                "service": service_name,
                "source": default_source,
                "message": f"{provider}:{user_email} missing scopes for {targets[0]}",
            }
        return {
            "success": True,
            "service": service_name,
            "source": default_source,
            "message": f"{provider}:{user_email} ready for {targets[0]}",
            "checks": [],
        }

    monkeypatch.setattr(service, "_google_target_test_result_for_stored_oauth", fake_target_result)

    result = await service.test_service("gmail")

    assert calls == ["first@example.com", "second@example.com"]
    assert result["success"] is True
    assert "second@example.com" in str(result["message"])


@pytest.mark.asyncio
async def test_test_service_tries_later_non_google_credential() -> None:
    oauth_service = AsyncMock()
    oauth_service.list_credentials.return_value = [
        {"provider": "slack", "user_email": "first@example.com", "is_expired": False},
        {"provider": "slack", "user_email": "second@example.com", "is_expired": False},
    ]
    oauth_service.test_credential = AsyncMock(
        side_effect=[
            {"valid": False, "error": "first credential invalid"},
            {"valid": True, "message": "second credential ready"},
        ]
    )
    service = UnifiedAuthService(oauth_service=oauth_service)

    result = await service.test_service("slack")

    called_emails = [
        call.kwargs["user_email"] for call in oauth_service.test_credential.await_args_list
    ]
    assert called_emails == ["first@example.com", "second@example.com"]
    assert result["valid"] is True
    assert result["service"] == "slack"
    assert "second credential ready" in str(result["message"])


@pytest.mark.asyncio
async def test_test_service_falls_back_to_native_after_target_oauth_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oauth_service = AsyncMock()
    oauth_service.list_credentials.return_value = [
        {"provider": "google", "user_email": "first@example.com", "is_expired": False},
    ]
    oauth_service.test_credential = AsyncMock(
        return_value={"valid": True, "message": "oauth credential loaded"}
    )
    service = UnifiedAuthService(oauth_service=oauth_service)
    native = {"source": "native:gws_cli", "email": "native@example.com", "message": "native ready"}
    monkeypatch.setattr(
        service, "_oauth_native_from_profile_store", lambda *_args, **_kwargs: native
    )

    async def fake_target_result_for_stored_oauth(
        service_name: str,
        targets: tuple[str, ...],
        *,
        provider: str,
        user_email: str,
        default_source: str,
    ) -> dict[str, object]:
        return {
            "success": False,
            "service": service_name,
            "source": default_source,
            "message": f"{provider}:{user_email} missing scopes for {targets[0]}",
        }

    async def fake_native_target_result(
        service_name: str,
        targets: tuple[str, ...],
        *,
        source: str,
        native: dict[str, str] | None = None,
        user_email: str | None = None,
        access_token: str | None = None,
    ) -> dict[str, object]:
        del native, access_token
        return {
            "success": True,
            "service": service_name,
            "source": source,
            "message": f"{user_email} native ready for {targets[0]}",
            "checks": [],
        }

    monkeypatch.setattr(
        service,
        "_google_target_test_result_for_stored_oauth",
        fake_target_result_for_stored_oauth,
    )
    monkeypatch.setattr(service, "_google_target_test_result", fake_native_target_result)

    result = await service.test_service("gmail")

    assert result["success"] is True
    assert result["source"] == "native:gws_cli"
    assert "native ready" in str(result["message"])
