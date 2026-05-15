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

    Walks the full exception chain (__cause__ then __context__) so that
    connectors which wrap raw SDK exceptions in BackendError still produce
    correct cooldown/retry decisions.  The raw Google exception is preserved
    as exc.__cause__ when ``raise BackendError(...) from e`` is used.

    Args:
        exc: Any exception raised by a Google API call or token refresh,
             including wrapped BackendError whose __cause__ is a Google exc.

    Returns:
        The matching AuthProfileFailureReason, or UNKNOWN as a fallback.
    """
    candidate: BaseException | None = exc
    seen: set[int] = set()
    while candidate is not None and id(candidate) not in seen:
        seen.add(id(candidate))
        if isinstance(candidate, Exception):
            result = _classify_single(candidate)
            if result != AuthProfileFailureReason.UNKNOWN:
                return result
        candidate = candidate.__cause__ or candidate.__context__
    return AuthProfileFailureReason.UNKNOWN


def _classify_single(exc: Exception) -> AuthProfileFailureReason:
    """Classify a single exception node (no chain-walking)."""
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
                # Resource not found — model access denied or model does not exist
                return AuthProfileFailureReason.UNKNOWN

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
            return str(errors[0].get("reason")) if errors[0].get("reason") is not None else None
    except Exception:
        pass
    return None
