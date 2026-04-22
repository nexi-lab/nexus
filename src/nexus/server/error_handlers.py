"""Error handler setup and exception-to-response mapping for FastAPI.

This module centralizes HTTP error handler functions that convert Nexus
exceptions into appropriate JSON responses with status codes and metadata.

Each NexusError subclass carries its own ``status_code`` and ``error_type``
class attributes, so this handler reads them directly instead of maintaining
a parallel isinstance chain. Adding a new exception subclass automatically
gets the correct HTTP mapping — no changes needed here.
"""

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


def nexus_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Custom handler for Nexus exceptions.

    Reads status_code and error_type from the exception class attributes
    (set on each NexusError subclass). Falls back to 500 for unknown errors.

    Includes is_expected flag for error classification:
    - Expected errors: User errors (validation, not found, permission denied)
    - Unexpected errors: System errors (backend failures, bugs)
    """
    status_code = getattr(exc, "status_code", 500)
    error_type = getattr(exc, "error_type", "Internal Server Error")
    is_expected = getattr(exc, "is_expected", False)
    path = getattr(exc, "path", None)

    content: dict[str, Any] = {
        "error": error_type,
        "detail": str(exc),
        "is_expected": is_expected,
    }
    if path:
        content["path"] = path

    # Add conflict-specific data (etag info for optimistic concurrency)
    expected_etag = getattr(exc, "expected_etag", None)
    if expected_etag is not None:
        content["expected_etag"] = expected_etag
        content["current_etag"] = getattr(exc, "current_etag", None)

    # Add authentication-specific data (provider, account, re-auth URL,
    # and machine-actionable recovery pointer for connector re-auth).
    for field in ("provider", "user_email", "auth_url", "recovery_hint"):
        val = getattr(exc, field, None)
        if val is not None:
            content[field] = val

    return JSONResponse(status_code=status_code, content=content)
