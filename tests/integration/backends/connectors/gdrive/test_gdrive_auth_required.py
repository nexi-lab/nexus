"""Regression tests for Issue #3822.

The gdrive connector must raise :class:`AuthenticationError` (with
``provider`` / ``user_email`` / ``auth_url`` populated when possible)
whenever a Drive operation is attempted without a valid OAuth token.
Silently returning ``[]`` from ``fs.ls`` masked missing tokens and left
no signal for callers to drive the OAuth flow.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.contracts.exceptions import AuthenticationError


def _build_transport(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Import the transport lazily to skip when google-api-python-client is absent."""
    pytest.importorskip("googleapiclient.discovery")
    from nexus.backends.connectors.gdrive.transport import DriveTransport

    # Env vars intentionally set; the transport no longer consumes them for
    # auth_url construction, but leaving them ensures we don't accidentally
    # start honoring them again without updating this test.
    monkeypatch.setenv("NEXUS_SERVER_URL", "http://localhost:4567")
    monkeypatch.setenv("NEXUS_OAUTH_REDIRECT_URI", "http://localhost:4567/callback")

    token_manager = MagicMock()
    return DriveTransport(token_manager=token_manager, provider="google-drive")


def test_get_drive_service_raises_authentication_error_when_no_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No user_email + no context -> AuthenticationError with populated provider, not BackendError.

    auth_url is intentionally None: connector credential recovery goes
    through the connector-scoped POST ``/v2/connectors/auth/init`` endpoint,
    not a GET-able OAuth URL mintable by a backend transport.
    """
    transport = _build_transport(monkeypatch)

    with pytest.raises(AuthenticationError) as exc_info:
        transport._get_drive_service()

    err = exc_info.value
    assert err.provider == "google-drive"
    assert err.user_email is None
    # Transport no longer mints an auth_url — see transport._build_auth_url
    # docstring for the reasoning (login-CSRF + wrong-endpoint risks).
    assert err.auth_url is None


def test_get_drive_service_raises_authentication_error_when_token_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """user_email present but token_manager has no valid token -> AuthenticationError."""
    transport = _build_transport(monkeypatch)
    transport._user_email = "user@example.com"

    # token_manager.get_valid_token raises AuthenticationError (missing credential)
    async def _raise(*_a: object, **_kw: object) -> None:
        raise AuthenticationError("No credential for google-drive:user@example.com")

    transport._token_manager.get_valid_token = _raise

    with pytest.raises(AuthenticationError) as exc_info:
        transport._get_drive_service()

    err = exc_info.value
    assert err.provider == "google-drive"
    assert err.user_email == "user@example.com"
    assert err.auth_url is None


def test_auth_url_is_always_none_regardless_of_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence-in-depth: auth_url is None even with every plausibly relevant env set."""
    pytest.importorskip("googleapiclient.discovery")
    from nexus.backends.connectors.gdrive.transport import DriveTransport

    monkeypatch.setenv("NEXUS_SERVER_URL", "http://localhost:4567")
    monkeypatch.setenv("NEXUS_OAUTH_REDIRECT_URI", "http://localhost:4567/callback")
    monkeypatch.setenv("NEXUS_OAUTH_GOOGLE_CLIENT_ID", "fake.apps.googleusercontent.com")
    monkeypatch.setenv("NEXUS_OAUTH_GOOGLE_CLIENT_SECRET", "fake")

    transport = DriveTransport(token_manager=MagicMock(), provider="google-drive")

    with pytest.raises(AuthenticationError) as exc_info:
        transport._get_drive_service()

    err = exc_info.value
    assert err.provider == "google-drive"
    assert err.auth_url is None


def test_connector_list_dir_propagates_authentication_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PathGDriveBackend.list_dir must not wrap AuthenticationError as BackendError.

    Previously the bare ``except Exception`` in ``list_dir`` caught the
    auth-required signal from the transport and re-raised it as
    :class:`BackendError`, which upstream ``sys_readdir`` then swallowed
    into an empty list.  The explicit ``except AuthenticationError``
    clause added for #3822 lets the signal bubble up with its
    ``provider`` / ``user_email`` / ``auth_url`` intact.
    """
    pytest.importorskip("googleapiclient.discovery")
    from nexus.backends.connectors.gdrive.connector import PathGDriveBackend

    monkeypatch.setenv("NEXUS_OAUTH_GOOGLE_CLIENT_ID", "fake.apps.googleusercontent.com")
    monkeypatch.setenv("NEXUS_OAUTH_GOOGLE_CLIENT_SECRET", "fake")

    connector = PathGDriveBackend(
        token_manager_db=":memory:",
        user_email="user@example.com",
        provider="google-drive",
    )

    # Swap the token manager so get_valid_token raises — simulates a mount
    # whose user has not yet completed the OAuth flow.
    async def _raise(*_a: object, **_kw: object) -> None:
        raise AuthenticationError("No credential for google-drive:user@example.com")

    connector.token_manager.get_valid_token = _raise
    connector._drive_transport._token_manager = connector.token_manager

    with pytest.raises(AuthenticationError) as exc_info:
        connector.list_dir("/")

    assert exc_info.value.provider == "google-drive"
    assert exc_info.value.user_email == "user@example.com"
