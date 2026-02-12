"""Structured error handling for the Nexus Exchange protocol.

Provides a unified error format following the google.rpc.Status pattern.
All Exchange API errors produce consistent JSON responses with machine-readable
error codes, human-readable messages, and optional diagnostic details.

Usage:
    from nexus.server.api.v2.error_handler import NexusExchangeError, NexusErrorCode

    raise NexusExchangeError(
        code=NexusErrorCode.INSUFFICIENT_BALANCE,
        message="Agent agent-123 has insufficient balance",
        details={"available": "10.00", "required": "25.00"},
    )
"""

from __future__ import annotations

import enum
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class NexusErrorCode(enum.IntEnum):
    """Domain-specific error codes for the Nexus Exchange protocol.

    Code ranges:
      0-999:     General errors
      1000-1999: Identity errors
      2000-2999: Payment errors
      3000-3999: Audit errors
      4000-4999: Exchange errors (reserved)
    """

    # General (0-999)
    UNSPECIFIED = 0
    INTERNAL = 1
    INVALID_ARGUMENT = 2
    NOT_FOUND = 3
    ALREADY_EXISTS = 4
    PERMISSION_DENIED = 5
    UNAUTHENTICATED = 6
    RATE_LIMITED = 7

    # Identity (1000-1999)
    AGENT_NOT_FOUND = 1000
    KEY_NOT_FOUND = 1001
    KEY_REVOKED = 1002
    KEY_EXPIRED = 1003
    SIGNATURE_INVALID = 1004

    # Payment (2000-2999)
    INSUFFICIENT_BALANCE = 2000
    TRANSFER_FAILED = 2001
    RESERVATION_NOT_FOUND = 2002
    RESERVATION_EXPIRED = 2003
    INVALID_AMOUNT = 2004
    WALLET_NOT_FOUND = 2005

    # Audit (3000-3999)
    RECORD_NOT_FOUND = 3000
    INTEGRITY_VIOLATION = 3001
    EXPORT_FAILED = 3002

    # Exchange (4000-4999)
    AUCTION_NOT_FOUND = 4000
    BID_REJECTED = 4001
    SETTLEMENT_FAILED = 4002


# Map error codes to HTTP status codes
_CODE_TO_HTTP_STATUS: dict[NexusErrorCode, int] = {
    NexusErrorCode.UNSPECIFIED: 500,
    NexusErrorCode.INTERNAL: 500,
    NexusErrorCode.INVALID_ARGUMENT: 400,
    NexusErrorCode.NOT_FOUND: 404,
    NexusErrorCode.ALREADY_EXISTS: 409,
    NexusErrorCode.PERMISSION_DENIED: 403,
    NexusErrorCode.UNAUTHENTICATED: 401,
    NexusErrorCode.RATE_LIMITED: 429,
    NexusErrorCode.AGENT_NOT_FOUND: 404,
    NexusErrorCode.KEY_NOT_FOUND: 404,
    NexusErrorCode.KEY_REVOKED: 410,
    NexusErrorCode.KEY_EXPIRED: 410,
    NexusErrorCode.SIGNATURE_INVALID: 401,
    NexusErrorCode.INSUFFICIENT_BALANCE: 402,
    NexusErrorCode.TRANSFER_FAILED: 500,
    NexusErrorCode.RESERVATION_NOT_FOUND: 404,
    NexusErrorCode.RESERVATION_EXPIRED: 410,
    NexusErrorCode.INVALID_AMOUNT: 400,
    NexusErrorCode.WALLET_NOT_FOUND: 404,
    NexusErrorCode.RECORD_NOT_FOUND: 404,
    NexusErrorCode.INTEGRITY_VIOLATION: 500,
    NexusErrorCode.EXPORT_FAILED: 500,
    NexusErrorCode.AUCTION_NOT_FOUND: 404,
    NexusErrorCode.BID_REJECTED: 409,
    NexusErrorCode.SETTLEMENT_FAILED: 500,
}


@dataclass(frozen=True)
class NexusExchangeError(Exception):
    """Structured error for Exchange API endpoints.

    Produces google.rpc.Status-compatible JSON when handled by the
    registered exception handler.
    """

    code: NexusErrorCode
    message: str
    details: dict[str, str] = field(default_factory=dict)
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    @property
    def http_status(self) -> int:
        """Map error code to HTTP status code."""
        return _CODE_TO_HTTP_STATUS.get(self.code, 500)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to google.rpc.Status-compatible JSON."""
        return {
            "error": {
                "code": self.code.name,
                "message": self.message,
                "details": self.details,
                "trace_id": self.trace_id,
            }
        }


async def _nexus_exchange_error_handler(
    _request: Request,
    exc: NexusExchangeError,
) -> JSONResponse:
    """FastAPI exception handler for NexusExchangeError."""
    if exc.http_status >= 500:
        logger.error(
            "Exchange error %s: %s (trace=%s)",
            exc.code.name,
            exc.message,
            exc.trace_id,
            extra={"error_code": exc.code.name, "trace_id": exc.trace_id},
        )
    else:
        logger.warning(
            "Exchange error %s: %s (trace=%s)",
            exc.code.name,
            exc.message,
            exc.trace_id,
            extra={"error_code": exc.code.name, "trace_id": exc.trace_id},
        )

    return JSONResponse(
        status_code=exc.http_status,
        content=exc.to_dict(),
    )


def register_exchange_error_handler(app: FastAPI) -> None:
    """Register the Exchange error handler on a FastAPI application.

    Call this during app startup to enable structured error responses
    for all Exchange API endpoints.
    """
    app.add_exception_handler(NexusExchangeError, _nexus_exchange_error_handler)  # type: ignore[arg-type]
