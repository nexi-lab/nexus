"""Boto3 / botocore error classifier for CredentialPool.

Maps botocore exceptions to AuthProfileFailureReason.
Covers S3, GCS-via-boto3, and other AWS SDK calls.

AWS error codes are structured in ClientError.response["Error"]["Code"].
Always use the code field (structured), never string-parse the message.
"""

from __future__ import annotations

from nexus.bricks.auth.profile import AuthProfileFailureReason

# AWS error codes that indicate permanent auth failure (key wrong/revoked).
_PERMANENT_AUTH_CODES: frozenset[str] = frozenset(
    {
        "AuthFailure",
        "InvalidClientTokenId",
        "InvalidAccessKeyId",
        "SignatureDoesNotMatch",
        "AccessDeniedException",
        "UnauthorizedOperation",
        "NotAuthorized",
    }
)

# AWS error codes that indicate the session/token has expired (re-auth needed).
_SESSION_EXPIRED_CODES: frozenset[str] = frozenset(
    {
        "ExpiredTokenException",
        "ExpiredToken",
        "RequestExpired",
        "TokenRefreshRequired",
    }
)

# AWS error codes that indicate throttling (1h cooldown, auto-recover).
_RATE_LIMIT_CODES: frozenset[str] = frozenset(
    {
        "ThrottlingException",
        "Throttling",
        "RequestLimitExceeded",
        "RequestThrottled",
        "TooManyRequestsException",
        "ProvisionedThroughputExceededException",
        "SlowDown",  # S3-specific
    }
)


def classify_boto3_error(exc: Exception) -> AuthProfileFailureReason:
    """Map a botocore exception to AuthProfileFailureReason.

    Args:
        exc: Any exception raised by a boto3 / botocore API call.

    Returns:
        The matching AuthProfileFailureReason, or UNKNOWN as a fallback.
    """
    try:
        import botocore.exceptions as botocore_exc
    except ImportError:
        return AuthProfileFailureReason.UNKNOWN

    if isinstance(exc, botocore_exc.EndpointConnectionError):
        return AuthProfileFailureReason.TIMEOUT

    if isinstance(exc, botocore_exc.ConnectTimeoutError):
        return AuthProfileFailureReason.TIMEOUT

    if isinstance(exc, botocore_exc.ReadTimeoutError):
        return AuthProfileFailureReason.TIMEOUT

    if isinstance(exc, botocore_exc.ClientError):
        # Structured error code — the authoritative signal
        error_code: str = exc.response.get("Error", {}).get("Code", "") if exc.response else ""

        if error_code in _PERMANENT_AUTH_CODES:
            return AuthProfileFailureReason.AUTH_PERMANENT

        if error_code in _SESSION_EXPIRED_CODES:
            return AuthProfileFailureReason.SESSION_EXPIRED

        if error_code in _RATE_LIMIT_CODES:
            return AuthProfileFailureReason.RATE_LIMIT

        # HTTP status fallback for unrecognised codes
        http_status: int = (
            exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) if exc.response else 0
        )
        if http_status in (500, 502, 503):
            return AuthProfileFailureReason.OVERLOADED
        if http_status == 429:
            return AuthProfileFailureReason.RATE_LIMIT
        if http_status in (401, 403):
            return AuthProfileFailureReason.AUTH_PERMANENT

    if isinstance(exc, botocore_exc.NoCredentialsError):
        # No credentials configured at all — surface to user
        return AuthProfileFailureReason.AUTH_PERMANENT

    if isinstance(exc, botocore_exc.PartialCredentialsError):
        return AuthProfileFailureReason.FORMAT

    return AuthProfileFailureReason.UNKNOWN
