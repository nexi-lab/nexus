"""Regression tests for OAuthCredentialService auth boundaries."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from nexus.bricks.auth.oauth.credential_service import OAuthCredentialService


@pytest.mark.asyncio
async def test_list_credentials_without_subject_fails_closed() -> None:
    token_manager = AsyncMock()
    token_manager.list_credentials = AsyncMock(return_value=[{"provider": "google"}])
    service = OAuthCredentialService(token_manager=token_manager)

    result = await service.list_credentials(include_revoked=True, context=None)

    assert result == [{"provider": "google"}]
    token_manager.list_credentials.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_credentials_admin_without_user_id_is_allowed() -> None:
    token_manager = AsyncMock()
    token_manager.list_credentials = AsyncMock(return_value=[{"provider": "google"}])
    service = OAuthCredentialService(token_manager=token_manager)
    context = cast(Any, SimpleNamespace(user_id=None, is_admin=True, zone_id="root"))

    result = await service.list_credentials(include_revoked=True, context=context)

    assert result == [{"provider": "google"}]
    token_manager.list_credentials.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_credentials_missing_user_id_context_fails_closed() -> None:
    token_manager = AsyncMock()
    token_manager.list_credentials = AsyncMock(return_value=[{"provider": "google"}])
    service = OAuthCredentialService(token_manager=token_manager)
    context = cast(Any, SimpleNamespace(user_id=None, is_admin=False, zone_id="root"))

    result = await service.list_credentials(include_revoked=True, context=context)

    assert result == []
    token_manager.list_credentials.assert_not_awaited()
