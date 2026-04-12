"""Google API error classifier for CredentialPool.

Maps Google API client exceptions to AuthProfileFailureReason.
Covers both the google-api-python-client (HttpError) and
google-auth exceptions (RefreshError, TransportError).

Google-specific notes:
  - Rate limit (429) may carry "quotaExceeded" for billing-style exhaustion.
    Use the structured 'reason' field from the error JSON body, not string parsing.
  - RefreshError indicates the OAuth token can no longer be refreshed —
    requires user re-authentication (SESSION_EXPIRED).
  - 403 can be "insufficientPermissions" (permanent) or "rateLimitExceeded" (transient).
"""

from __future__ import annotations

from typing import Any

from nexus.bricks.auth.profile import AuthProfileFailureReason


def classify_google_error(exc: Exception) -> AuthProfileFailureReason:
    """Map a Google API / google-auth exception to AuthProfileFailureReason.

    Args:
        exc: Any exception raised by a Google API call or token refresh.

    Returns:
        The matching AuthProfileFailureReason, or UNKNOWN as a fallback.
    """
    # google-auth refresh / transport errors
    try:
        from google.auth import exceptions as google_auth_exc

        if isinstance(exc, google_auth_exc.RefreshError):
            # OAuth token expired and cannot be refreshed — user must re-auth
            return AuthProfileFailureReason.SESSION_EXPIRED
        if isinstance(exc, google_auth_exc.TransportError):
            return AuthProfileFailureReason.TIMEOUT
    except ImportError:
        pass

    # google-api-python-client HttpError
    try:
        from googleapiclient.errors import HttpError

        if isinstance(exc, HttpError):
            status = exc.resp.status if exc.resp else 0

            if status == 401:
                return AuthProfileFailureReason.AUTH_PERMANENT

            if status == 403:
                # Distinguish permanent permission denial from transient rate-limit.
                # Use structured 'reason' from error JSON body (not string parsing).
                reason = _extract_google_error_reason(exc)
                if reason in ("rateLimitExceeded", "userRateLimitExceeded"):
                    return AuthProfileFailureReason.RATE_LIMIT
                if reason == "quotaExceeded":
                    return AuthProfileFailureReason.BILLING
                # Default 403: permission denied — permanent
                return AuthProfileFailureReason.AUTH_PERMANENT

            if status == 429:
                reason = _extract_google_error_reason(exc)
                if reason == "quotaExceeded":
                    return AuthProfileFailureReason.BILLING
                return AuthProfileFailureReason.RATE_LIMIT

            if status in (500, 502, 503):
                return AuthProfileFailureReason.OVERLOADED

            if status == 404:
                return AuthProfileFailureReason.MODEL_NOT_FOUND

            if status == 400:
                return AuthProfileFailureReason.FORMAT

    except ImportError:
        pass

    # Network-level timeouts
    try:
        import requests

        if isinstance(exc, requests.exceptions.Timeout):
            return AuthProfileFailureReason.TIMEOUT
        if isinstance(exc, requests.exceptions.ConnectionError):
            return AuthProfileFailureReason.TIMEOUT
    except ImportError:
        pass

    return AuthProfileFailureReason.UNKNOWN


def _extract_google_error_reason(exc: Any) -> str | None:
    """Extract the structured 'reason' field from a Google HttpError body.

    Google error bodies look like:
        {"error": {"errors": [{"reason": "rateLimitExceeded", ...}], ...}}

    Returns the reason string, or None if not parseable.
    """
    import json

    try:
        body = exc.content
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        parsed = json.loads(body)
        errors = parsed.get("error", {}).get("errors", [])
        if errors:
            return errors[0].get("reason")
    except Exception:
        pass
    return None
