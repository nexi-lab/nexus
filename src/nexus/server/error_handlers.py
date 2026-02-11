"""Error handler setup and exception-to-response mapping for FastAPI.

This module centralizes HTTP error handler functions that convert Nexus
exceptions into appropriate JSON responses with status codes and metadata.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from nexus.core.exceptions import (
    StaleSessionError,
)


def nexus_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Custom handler for Nexus exceptions.

    Includes is_expected flag for error classification:
    - Expected errors: User errors (validation, not found, permission denied)
    - Unexpected errors: System errors (backend failures, bugs)
    """
    from nexus.core.exceptions import (
        AuthenticationError,
        BackendError,
        ConflictError,
        InvalidPathError,
        NexusError,
        NexusFileNotFoundError,
        NexusPermissionError,
        ParserError,
        PermissionDeniedError,
        ValidationError,
    )

    # Determine HTTP status code and error type based on exception
    if isinstance(exc, NexusFileNotFoundError):
        status_code = 404
        error_type = "Not Found"
    elif isinstance(exc, (NexusPermissionError, PermissionDeniedError)):
        status_code = 403
        error_type = "Forbidden"
    elif isinstance(exc, AuthenticationError):
        status_code = 401
        error_type = "Unauthorized"
    elif isinstance(exc, (InvalidPathError, ValidationError)):
        status_code = 400
        error_type = "Bad Request"
    elif isinstance(exc, (ConflictError, StaleSessionError)):
        status_code = 409
        error_type = "Conflict"
    elif isinstance(exc, ParserError):
        status_code = 422
        error_type = "Unprocessable Entity"
    elif isinstance(exc, BackendError):
        status_code = 502
        error_type = "Bad Gateway"
    elif isinstance(exc, NexusError):
        status_code = 500
        error_type = "Internal Server Error"
    else:
        status_code = 500
        error_type = "Internal Server Error"

    is_expected = getattr(exc, "is_expected", False)
    path = getattr(exc, "path", None)

    content: dict[str, Any] = {
        "error": error_type,
        "detail": str(exc),
        "is_expected": is_expected,
    }
    if path:
        content["path"] = path

    # Add conflict-specific data
    if isinstance(exc, ConflictError):
        content["expected_etag"] = exc.expected_etag
        content["current_etag"] = exc.current_etag

    return JSONResponse(status_code=status_code, content=content)
