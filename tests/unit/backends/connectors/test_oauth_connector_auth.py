"""Connector-agnostic OAuth auth-required behaviour (Issue #3822 follow-up).

Every OAuth connector transport (gmail, gdrive, gcalendar, slack, x, …) must
surface missing / expired credentials as :class:`AuthenticationError` with a
populated ``recovery_hint`` — not as ``BackendError`` or a silent empty
listing.  Earlier releases only fixed gdrive; nexus silent-empty reports
against gmail/slack/x showed the rest of the connectors still masked auth
failures.  The shared helper lives in
``nexus.backends.connectors.oauth_base``; this module exercises each
transport's wrapper path against both failure modes:

* ``user_email is None`` — nothing to resolve.
* ``token_manager.get_valid_token`` raises ``AuthenticationError`` — the
  transport must re-raise with full structured payload.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.backends.connectors.oauth_base import (
    build_auth_recovery_hint,
    resolve_oauth_access_token,
)
from nexus.contracts.exceptions import AuthenticationError

# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


class TestBuildAuthRecoveryHint:
    def test_matches_auth_init_request_schema(self) -> None:
        hint = build_auth_recovery_hint(
            connector_name="gmail_connector",
            provider="gmail",
            user_email="u@example.com",
        )
        assert hint["endpoint"] == "/api/v2/connectors/auth/init"
        assert hint["method"] == "POST"
        assert hint["connector_name"] == "gmail_connector"
        assert hint["provider"] == "gmail"
        assert hint["user_email"] == "u@example.com"

    def test_omits_user_email_when_unknown(self) -> None:
        hint = build_auth_recovery_hint(connector_name="x_connector", provider="x")
        assert "user_email" not in hint


class TestResolveOAuthAccessToken:
    def test_missing_user_email_raises_authentication_error(self) -> None:
        tm = MagicMock()
        with pytest.raises(AuthenticationError) as exc_info:
            resolve_oauth_access_token(
                tm,
                connector_name="gmail_connector",
                provider="gmail",
                user_email=None,
            )
        err = exc_info.value
        assert err.provider == "gmail"
        assert err.user_email is None
        assert err.recovery_hint is not None
        assert err.recovery_hint["connector_name"] == "gmail_connector"
        assert err.recovery_hint["endpoint"] == "/api/v2/connectors/auth/init"

    def test_token_manager_auth_error_is_reraised_with_hint(self) -> None:
        async def _fail(*_a: object, **_kw: object) -> str:
            raise AuthenticationError("Token expired")

        tm = MagicMock()
        tm.get_valid_token = _fail
        with pytest.raises(AuthenticationError) as exc_info:
            resolve_oauth_access_token(
                tm,
                connector_name="slack_connector",
                provider="slack",
                user_email="user@example.com",
            )
        err = exc_info.value
        assert err.provider == "slack"
        assert err.user_email == "user@example.com"
        assert err.recovery_hint is not None
        assert err.recovery_hint["connector_name"] == "slack_connector"
        assert err.recovery_hint["user_email"] == "user@example.com"

    def test_non_auth_errors_propagate_unchanged(self) -> None:
        """Network / misconfig failures must not be rewrapped as auth errors —
        clients would mistake transient problems for recoverable 401s."""

        async def _network_fail(*_a: object, **_kw: object) -> str:
            raise ConnectionError("DNS lookup failed")

        tm = MagicMock()
        tm.get_valid_token = _network_fail
        with pytest.raises(ConnectionError, match="DNS lookup failed"):
            resolve_oauth_access_token(
                tm,
                connector_name="gcalendar_connector",
                provider="google",
                user_email="user@example.com",
            )

    def test_happy_path_returns_token(self) -> None:
        async def _ok(*_a: object, **_kw: object) -> str:
            return "ya29.fake-token"

        tm = MagicMock()
        tm.get_valid_token = _ok
        token = resolve_oauth_access_token(
            tm,
            connector_name="gdrive_connector",
            provider="google-drive",
            user_email="user@example.com",
        )
        assert token == "ya29.fake-token"

    def test_blank_user_email_does_not_block_request_identity(self) -> None:
        """Mount configs sometimes render ``user_email: ""`` for "no pin".
        Treating that as an authoritative empty pin would lock every
        request out with a false 401 — must normalise to ``None`` and
        fall through to ``nexus_user_id`` resolution.
        """
        captured: dict[str, object] = {}

        async def _ok(**kwargs: object) -> str:
            captured.update(kwargs)
            return "ya29.fake-token"

        tm = MagicMock()
        tm.get_valid_token = _ok
        token = resolve_oauth_access_token(
            tm,
            connector_name="gdrive_connector",
            provider="google-drive",
            user_email="",  # blank pin — should be treated as absent
            nexus_user_id="req@example.com",
        )
        assert token == "ya29.fake-token"
        assert captured["user_email"] == "req@example.com"

    def test_malformed_credential_raises_with_full_recovery_hint(self) -> None:
        """When every credential row for this user + provider has a blank
        or malformed ``user_email``, the resolver must raise a distinct
        ``AuthenticationError`` whose ``recovery_hint`` is a superset of
        the standard auth-init contract (endpoint / method /
        connector_name / provider) with ``action=relink_credential`` and
        ``user_id`` extensions — so clients can auto-drive recovery
        without falling through to manual troubleshooting.
        """

        async def _list_creds(**_kwargs: object) -> list[dict[str, object]]:
            return [
                {"provider": "google-drive", "user_email": ""},
                {"provider": "google-drive", "user_email": None},
            ]

        tm = MagicMock()
        tm.list_credentials = _list_creds
        # get_valid_token must never be reached on this path.
        tm.get_valid_token = MagicMock(
            side_effect=AssertionError("get_valid_token should not be called")
        )
        with pytest.raises(AuthenticationError) as exc_info:
            resolve_oauth_access_token(
                tm,
                connector_name="gdrive_connector",
                provider="google-drive",
                user_email=None,
                nexus_user_id="admin",
            )
        err = exc_info.value
        assert err.provider == "google-drive"
        assert err.recovery_hint is not None
        assert err.recovery_hint["endpoint"] == "/api/v2/connectors/auth/init"
        assert err.recovery_hint["method"] == "POST"
        assert err.recovery_hint["connector_name"] == "gdrive_connector"
        assert err.recovery_hint["provider"] == "google-drive"
        assert err.recovery_hint["action"] == "relink_credential"
        assert err.recovery_hint["user_id"] == "admin"

    def test_whitespace_user_email_does_not_block_request_identity(self) -> None:
        """Same guard as the blank case — whitespace-only is absent too."""
        captured: dict[str, object] = {}

        async def _ok(**kwargs: object) -> str:
            captured.update(kwargs)
            return "ya29.fake-token"

        tm = MagicMock()
        tm.get_valid_token = _ok
        token = resolve_oauth_access_token(
            tm,
            connector_name="gdrive_connector",
            provider="google-drive",
            user_email="   ",
            nexus_user_id="req@example.com",
        )
        assert token == "ya29.fake-token"
        assert captured["user_email"] == "req@example.com"

    def test_email_shaped_nexus_user_id_resolves_without_index_lookup(self) -> None:
        """An email-shaped ``nexus_user_id`` should flow straight to
        ``get_valid_token`` — some deployments issue API keys whose
        subject *is* the user's email, and credential rows in legacy /
        mixed-issuance paths may be keyed only by ``user_email`` with no
        secondary ``user_id`` row, so a credential-index lookup here
        would miss and the function would emit a false 401.
        """
        captured: dict[str, object] = {}

        async def _ok(**kwargs: object) -> str:
            captured.update(kwargs)
            return "ya29.fake-token"

        tm = MagicMock()
        tm.get_valid_token = _ok
        # list_credentials must NOT be consulted on this path.
        tm.list_credentials = MagicMock(
            side_effect=AssertionError("list_credentials should not be called")
        )
        token = resolve_oauth_access_token(
            tm,
            connector_name="gdrive_connector",
            provider="google-drive",
            user_email=None,
            nexus_user_id="user@example.com",
        )
        assert token == "ya29.fake-token"
        assert captured["user_email"] == "user@example.com"


# ---------------------------------------------------------------------------
# Per-connector transport wiring — each must route through the helper
# ---------------------------------------------------------------------------


def _auth_error_on_token() -> MagicMock:
    async def _raise(*_a: object, **_kw: object) -> str:
        raise AuthenticationError("No credential stored")

    tm = MagicMock()
    tm.get_valid_token = _raise
    return tm


class TestGdriveTransportAuthPropagation:
    def test_missing_user_email(self) -> None:
        pytest.importorskip("googleapiclient.discovery")
        from nexus.backends.connectors.gdrive.transport import DriveTransport

        transport = DriveTransport(token_manager=MagicMock(), provider="google-drive")
        with pytest.raises(AuthenticationError) as exc_info:
            transport._get_drive_service()
        err = exc_info.value
        assert err.provider == "google-drive"
        assert err.recovery_hint is not None
        assert err.recovery_hint["connector_name"] == "gdrive_connector"

    def test_token_manager_auth_error(self) -> None:
        pytest.importorskip("googleapiclient.discovery")
        from nexus.backends.connectors.gdrive.transport import DriveTransport

        transport = DriveTransport(token_manager=_auth_error_on_token(), provider="google-drive")
        transport._user_email = "user@example.com"
        with pytest.raises(AuthenticationError) as exc_info:
            transport._get_drive_service()
        err = exc_info.value
        assert err.provider == "google-drive"
        assert err.user_email == "user@example.com"
        assert err.recovery_hint is not None
        assert err.recovery_hint["connector_name"] == "gdrive_connector"


class TestGmailTransportAuthPropagation:
    def test_missing_user_email(self) -> None:
        pytest.importorskip("googleapiclient.discovery")
        from nexus.backends.connectors.gmail.transport import GmailTransport

        transport = GmailTransport(token_manager=MagicMock(), provider="gmail")
        with pytest.raises(AuthenticationError) as exc_info:
            transport._get_gmail_service()
        err = exc_info.value
        assert err.provider == "gmail"
        assert err.recovery_hint is not None
        assert err.recovery_hint["connector_name"] == "gmail_connector"

    def test_token_manager_auth_error(self) -> None:
        pytest.importorskip("googleapiclient.discovery")
        from nexus.backends.connectors.gmail.transport import GmailTransport

        transport = GmailTransport(token_manager=_auth_error_on_token(), provider="gmail")
        transport._user_email = "user@example.com"
        with pytest.raises(AuthenticationError) as exc_info:
            transport._get_gmail_service()
        err = exc_info.value
        assert err.user_email == "user@example.com"
        assert err.recovery_hint is not None
        assert err.recovery_hint["connector_name"] == "gmail_connector"


class TestCalendarTransportAuthPropagation:
    def test_missing_user_email(self) -> None:
        pytest.importorskip("googleapiclient.discovery")
        from nexus.backends.connectors.calendar.transport import CalendarTransport

        transport = CalendarTransport(token_manager=MagicMock(), provider="google")
        with pytest.raises(AuthenticationError) as exc_info:
            transport._get_calendar_service()
        err = exc_info.value
        assert err.recovery_hint is not None
        assert err.recovery_hint["connector_name"] == "gcalendar_connector"


class TestSlackTransportAuthPropagation:
    def test_missing_user_email(self) -> None:
        pytest.importorskip("slack_sdk")
        from nexus.backends.connectors.slack.transport import SlackTransport

        transport = SlackTransport(token_manager=MagicMock(), provider="slack")
        with pytest.raises(AuthenticationError) as exc_info:
            transport._get_slack_client()
        err = exc_info.value
        assert err.provider == "slack"
        assert err.recovery_hint is not None
        assert err.recovery_hint["connector_name"] == "slack_connector"


class TestXTransportAuthPropagation:
    def test_missing_user_email(self) -> None:
        from nexus.backends.connectors.x.transport import XTransport
        from nexus.lib.sync_bridge import run_sync

        transport = XTransport(token_manager=MagicMock(), provider="x")
        with pytest.raises(AuthenticationError) as exc_info:
            run_sync(transport._get_api_client_async())
        err = exc_info.value
        assert err.provider == "x"
        assert err.recovery_hint is not None
        assert err.recovery_hint["connector_name"] == "x_connector"

    def test_token_manager_auth_error(self) -> None:
        from nexus.backends.connectors.x.transport import XTransport
        from nexus.lib.sync_bridge import run_sync

        transport = XTransport(token_manager=_auth_error_on_token(), provider="x")
        transport._user_email = "user@example.com"
        with pytest.raises(AuthenticationError) as exc_info:
            run_sync(transport._get_api_client_async())
        err = exc_info.value
        assert err.user_email == "user@example.com"
        assert err.recovery_hint is not None
        assert err.recovery_hint["connector_name"] == "x_connector"

    def test_cache_principal_is_zone_and_email_scoped(self) -> None:
        """Two zones sharing a nexus subject id (e.g. ``admin``) must not
        share cache entries — the principal key must be derived from the
        resolved OAuth identity + zone, never the raw ``context.user_id``.
        Otherwise zone A's cached X timeline would be served to zone B.
        """
        from nexus.backends.connectors.x.transport import XTransport

        p1 = XTransport._cache_principal("alice@example.com", "zone-a")
        p2 = XTransport._cache_principal("alice@example.com", "zone-b")
        p3 = XTransport._cache_principal("bob@example.com", "zone-a")
        assert p1 != p2, "cache principal must vary with zone"
        assert p1 != p3, "cache principal must vary with resolved email"
        # Regression: the raw nexus subject ID (e.g. "admin") must not
        # reduce to the same principal as any real zone/email pair.
        assert "admin" not in (p1, p2, p3)
