"""Anthropic error classifier for CredentialPool.

Maps anthropic SDK exceptions to AuthProfileFailureReason.

Key Anthropic-specific difference from OpenAI:
  - HTTP 529 "OverloadedError" is Anthropic's overload signal (not 503).
  - Billing exhaustion surfaces as a 400 BadRequestError with
    error.type == "invalid_request_error" and a quota message,
    NOT as a RateLimitError. Use error.type (structured), not string parsing.
"""

from __future__ import annotations

from nexus.bricks.auth.profile import AuthProfileFailureReason


def classify_anthropic_error(exc: Exception) -> AuthProfileFailureReason:
    """Map an anthropic SDK exception to AuthProfileFailureReason.

    Args:
        exc: Any exception raised by an anthropic API call.

    Returns:
        The matching AuthProfileFailureReason, or UNKNOWN as a fallback.
    """
    try:
        import anthropic
    except ImportError:
        return AuthProfileFailureReason.UNKNOWN

    if isinstance(exc, anthropic.AuthenticationError):
        # 401 — key wrong or revoked
        return AuthProfileFailureReason.AUTH_PERMANENT

    if isinstance(exc, anthropic.PermissionDeniedError):
        # 403 — key lacks permission
        return AuthProfileFailureReason.AUTH_PERMANENT

    if isinstance(exc, anthropic.RateLimitError):
        # 429 — per-minute throttling; Anthropic does not embed billing codes here
        return AuthProfileFailureReason.RATE_LIMIT

    if isinstance(exc, anthropic.OverloadedError):
        # 529 — Anthropic-specific overload signal; distinct from 5xx server errors
        return AuthProfileFailureReason.OVERLOADED

    if isinstance(exc, anthropic.InternalServerError):
        # 500/503 — server-side; retry soon
        return AuthProfileFailureReason.OVERLOADED

    if isinstance(exc, anthropic.APITimeoutError):
        return AuthProfileFailureReason.TIMEOUT

    if isinstance(exc, anthropic.APIConnectionError):
        return AuthProfileFailureReason.TIMEOUT

    if isinstance(exc, anthropic.NotFoundError):
        # 404 on model endpoint — model access denied or model does not exist
        return AuthProfileFailureReason.UNKNOWN

    if isinstance(exc, anthropic.BadRequestError):
        # 400 — could be billing exhaustion or malformed request.
        # Check error.type for the billing subcase (structured, not string parse).
        err_type = getattr(getattr(exc, "error", None), "type", None)
        if err_type == "invalid_request_error":
            # Check body for quota signal — structured field preferred
            body = getattr(exc, "body", None) or {}
            if isinstance(body, dict):
                err_detail = body.get("error", {})
                if (
                    isinstance(err_detail, dict)
                    and "quota" in str(err_detail.get("message", "")).lower()
                ):
                    return AuthProfileFailureReason.BILLING
        return AuthProfileFailureReason.FORMAT

    return AuthProfileFailureReason.UNKNOWN
