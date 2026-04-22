"""Slack SDK error classifier for CredentialPool.

Maps slack_sdk exceptions to AuthProfileFailureReason.

Slack uses a unified SlackApiError with a structured error code in
response["error"]. Always use the code (structured), not the message string.

Common error codes:
  not_authed, invalid_auth, account_inactive, token_revoked → AUTH_PERMANENT
  token_expired → SESSION_EXPIRED (token can be refreshed, user re-auth needed)
  ratelimited → RATE_LIMIT
  fatal_error, internal_error → OVERLOADED
"""

from __future__ import annotations

from nexus.bricks.auth.profile import AuthProfileFailureReason

# Slack error codes that indicate the token is permanently invalid.
_PERMANENT_AUTH_CODES: frozenset[str] = frozenset(
    {
        "not_authed",
        "invalid_auth",
        "account_inactive",
        "token_revoked",
        "no_permission",
        "missing_scope",
        "team_access_not_granted",
        "org_login_required",
    }
)

# Slack error codes that require user re-authentication (token can be refreshed).
_SESSION_EXPIRED_CODES: frozenset[str] = frozenset(
    {
        "token_expired",
        "ekm_access_denied",
    }
)


def classify_slack_error(exc: Exception) -> AuthProfileFailureReason:
    """Map a slack_sdk exception to AuthProfileFailureReason.

    Args:
        exc: Any exception raised by a Slack API call.

    Returns:
        The matching AuthProfileFailureReason, or UNKNOWN as a fallback.
    """
    try:
        from slack_sdk.errors import SlackApiError, SlackRequestError
    except ImportError:
        return AuthProfileFailureReason.UNKNOWN

    if isinstance(exc, SlackRequestError):
        # Network-level failure (connection refused, timeout)
        return AuthProfileFailureReason.TIMEOUT

    if isinstance(exc, SlackApiError):
        # Structured error code from Slack's API response
        error_code: str = exc.response.get("error", "") if exc.response else ""

        if error_code in _PERMANENT_AUTH_CODES:
            return AuthProfileFailureReason.AUTH_PERMANENT

        if error_code in _SESSION_EXPIRED_CODES:
            return AuthProfileFailureReason.SESSION_EXPIRED

        if error_code == "ratelimited":
            return AuthProfileFailureReason.RATE_LIMIT

        if error_code in ("fatal_error", "internal_error", "service_unavailable"):
            return AuthProfileFailureReason.OVERLOADED

        # Check HTTP status as fallback for codes we don't recognise
        http_status = exc.response.status_code if exc.response else 0
        if http_status == 429:
            return AuthProfileFailureReason.RATE_LIMIT
        if http_status in (500, 502, 503):
            return AuthProfileFailureReason.OVERLOADED
        if http_status == 401:
            return AuthProfileFailureReason.AUTH_PERMANENT

    return AuthProfileFailureReason.UNKNOWN
