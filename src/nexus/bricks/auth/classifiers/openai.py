"""OpenAI error classifier for CredentialPool.

Maps openai SDK exceptions to AuthProfileFailureReason so the pool can
apply the correct cooldown and retry policy without inspecting raw HTTP status.

Design note: uses exc.code (structured SDK field) rather than string-parsing
exc.message for the billing/rate-limit disambiguation. The openai SDK sets
code="insufficient_quota" for 402-equivalent exhausted-quota errors and
code="rate_limit_exceeded" for 429 per-minute throttling.
String parsing of error messages is fragile across SDK versions.
"""

from __future__ import annotations

from nexus.bricks.auth.profile import AuthProfileFailureReason


def classify_openai_error(exc: Exception) -> AuthProfileFailureReason:
    """Map an openai SDK exception to AuthProfileFailureReason.

    Args:
        exc: Any exception raised by an openai API call.

    Returns:
        The matching AuthProfileFailureReason, or UNKNOWN as a fallback.
    """
    try:
        import openai
    except ImportError:
        return AuthProfileFailureReason.UNKNOWN

    if isinstance(exc, openai.AuthenticationError):
        # 401 — key is wrong or revoked; no auto-recovery
        return AuthProfileFailureReason.AUTH_PERMANENT

    if isinstance(exc, openai.PermissionDeniedError):
        # 403 — key exists but lacks the required permission
        return AuthProfileFailureReason.AUTH_PERMANENT

    if isinstance(exc, openai.RateLimitError):
        # 429 — two distinct sub-cases with different cooldown policies:
        #   code="insufficient_quota"  → billing exhaustion (24h cooldown, manual review)
        #   anything else              → per-minute rate limit (1h cooldown, auto-recover)
        # Use exc.code (structured) not str(exc) (brittle across SDK versions).
        if getattr(exc, "code", None) == "insufficient_quota":
            return AuthProfileFailureReason.BILLING
        return AuthProfileFailureReason.RATE_LIMIT

    if isinstance(exc, openai.APITimeoutError):
        return AuthProfileFailureReason.TIMEOUT

    if isinstance(exc, openai.APIConnectionError):
        # Network-level failure — treat same as timeout
        return AuthProfileFailureReason.TIMEOUT

    if isinstance(exc, openai.InternalServerError):
        # 500/503 — provider-side; retry soon
        return AuthProfileFailureReason.OVERLOADED

    if isinstance(exc, openai.NotFoundError):
        # 404 on model endpoint — model access denied or model does not exist
        return AuthProfileFailureReason.UNKNOWN

    if isinstance(exc, openai.BadRequestError):
        # 400 — malformed request; not an auth/rate issue
        return AuthProfileFailureReason.FORMAT

    return AuthProfileFailureReason.UNKNOWN
