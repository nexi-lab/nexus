"""Nexus Pay REST API endpoints.

Provides 8 endpoints for agent payment operations:
- GET    /api/v2/pay/balance                     - Get agent balance
- GET    /api/v2/pay/can-afford                  - Check if agent can afford amount
- POST   /api/v2/pay/transfer                    - Single transfer (auto-routes credits/x402)
- POST   /api/v2/pay/transfer/batch              - Atomic batch transfer
- POST   /api/v2/pay/reserve                     - Reserve credits (two-phase)
- POST   /api/v2/pay/reserve/{id}/commit         - Commit reservation
- POST   /api/v2/pay/reserve/{id}/release        - Release reservation
- POST   /api/v2/pay/meter                       - Record metered usage

Related: Issue #1209
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/pay", tags=["pay"])

# Maximum decimal places for monetary amounts (matches MICRO_UNIT_SCALE = 1_000_000)
MAX_DECIMAL_PLACES = 6

# Maximum items in a batch transfer
MAX_BATCH_SIZE = 1000

# =============================================================================
# Pydantic Request/Response Models
# =============================================================================


def _validate_amount(v: str) -> str:
    """Validate a monetary amount string: positive, <=6 decimal places."""
    try:
        dec = Decimal(v)
    except InvalidOperation:
        raise ValueError(f"Invalid amount: {v!r}") from None
    if dec <= 0:
        raise ValueError("Amount must be positive")
    if dec.as_tuple().exponent is not None and abs(int(dec.as_tuple().exponent)) > MAX_DECIMAL_PLACES:
        raise ValueError(f"Amount must have at most {MAX_DECIMAL_PLACES} decimal places")
    return v


class TransferRequestModel(BaseModel):
    """Request to transfer credits."""

    to: str = Field(..., description="Recipient agent ID or wallet address")
    amount: str = Field(..., description="Amount as decimal string (e.g. '10.50')")
    memo: str = Field(default="", description="Optional memo/description")
    idempotency_key: str | None = Field(default=None, description="Optional idempotency key for retry safety")
    method: str = Field(default="auto", description="Payment method: 'auto', 'credits', or 'x402'")

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v: str) -> str:
        return _validate_amount(v)

    @field_validator("method")
    @classmethod
    def validate_method(cls, v: str) -> str:
        if v not in ("auto", "credits", "x402"):
            raise ValueError("method must be 'auto', 'credits', or 'x402'")
        return v


class BatchTransferItemModel(BaseModel):
    """A single transfer in a batch."""

    to: str = Field(..., description="Recipient agent ID")
    amount: str = Field(..., description="Amount as decimal string")
    memo: str = Field(default="", description="Optional memo")

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v: str) -> str:
        return _validate_amount(v)


class BatchTransferRequestModel(BaseModel):
    """Request for atomic batch transfer."""

    transfers: list[BatchTransferItemModel] = Field(
        ...,
        description="List of transfers to execute atomically",
        max_length=MAX_BATCH_SIZE,
    )


class ReserveRequestModel(BaseModel):
    """Request to reserve credits."""

    amount: str = Field(..., description="Amount to reserve as decimal string")
    timeout: int = Field(default=300, ge=1, le=86400, description="Auto-release timeout in seconds (1-86400)")
    purpose: str = Field(default="general", description="Purpose of reservation")
    task_id: str | None = Field(default=None, description="Optional task identifier")

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v: str) -> str:
        return _validate_amount(v)


class CommitRequestModel(BaseModel):
    """Request to commit a reservation."""

    actual_amount: str | None = Field(
        default=None,
        description="Actual amount to charge (None = full reserved amount)",
    )

    @field_validator("actual_amount")
    @classmethod
    def validate_amount(cls, v: str | None) -> str | None:
        if v is not None:
            return _validate_amount(v)
        return v


class MeterRequestModel(BaseModel):
    """Request to record metered usage."""

    amount: str = Field(..., description="Amount to deduct as decimal string")
    event_type: str = Field(default="api_call", description="Type of metered event")

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v: str) -> str:
        return _validate_amount(v)


# --- Response Models ---


class BalanceResponse(BaseModel):
    """Agent balance information."""

    available: str = Field(..., description="Available balance")
    reserved: str = Field(..., description="Reserved (pending) balance")
    total: str = Field(..., description="Total balance (available + reserved)")


class ReceiptResponse(BaseModel):
    """Receipt for a completed payment."""

    id: str = Field(..., description="Transaction ID")
    method: str = Field(..., description="Payment method ('credits' or 'x402')")
    amount: str = Field(..., description="Amount transferred")
    from_agent: str = Field(..., description="Sender agent ID")
    to_agent: str = Field(..., description="Recipient agent ID or address")
    memo: str | None = Field(default=None, description="Transaction memo")
    timestamp: str | None = Field(default=None, description="ISO 8601 timestamp")
    tx_hash: str | None = Field(default=None, description="Blockchain tx hash (x402 only)")


class ReservationResponse(BaseModel):
    """A pending credit reservation."""

    id: str = Field(..., description="Reservation ID")
    amount: str = Field(..., description="Reserved amount")
    purpose: str = Field(..., description="Reservation purpose")
    expires_at: str | None = Field(default=None, description="Auto-release time (ISO 8601)")
    status: str = Field(..., description="Status: 'pending', 'committed', or 'released'")


class CanAffordResponse(BaseModel):
    """Affordability check result."""

    can_afford: bool = Field(..., description="Whether agent can afford the amount")
    amount: str = Field(..., description="Amount checked")


class MeterResponse(BaseModel):
    """Metering result."""

    success: bool = Field(..., description="Whether deduction succeeded")


class ErrorResponse(BaseModel):
    """Error response."""

    detail: str = Field(..., description="Error message")
    error_code: str = Field(..., description="Error code for programmatic handling")


# =============================================================================
# Dependencies
# =============================================================================


def _get_require_auth() -> Any:
    """Lazy import to avoid circular imports."""
    from nexus.server.fastapi_server import require_auth

    return require_auth


def _get_credits_service(request: Request) -> Any:
    """Get CreditsService from app state."""
    service = getattr(request.app.state, "credits_service", None)
    if not service:
        raise HTTPException(
            status_code=503,
            detail="Credits service not available",
        )
    return service


def _get_x402_client(request: Request) -> Any:
    """Get X402Client from app state (may be None)."""
    return getattr(request.app.state, "x402_client", None)


def _extract_agent_id(auth_result: dict[str, Any]) -> str:
    """Extract agent_id from auth result.

    Priority: x_agent_id header > subject_id (for agents) > subject_id (for users).
    """
    subject_type = auth_result.get("subject_type", "user")
    subject_id = auth_result.get("subject_id", "anonymous")
    x_agent_id = auth_result.get("x_agent_id")

    if x_agent_id:
        return x_agent_id
    if subject_type == "agent":
        return subject_id
    return subject_id


async def get_nexuspay(
    request: Request,
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> Any:
    """Construct a NexusPay SDK instance per-request from auth context."""
    from nexus.pay.sdk import NexusPay

    agent_id = _extract_agent_id(auth_result)
    zone_id = auth_result.get("zone_id", "default")

    return NexusPay(
        api_key=f"nx_live_{agent_id}",
        credits_service=_get_credits_service(request),
        x402_client=_get_x402_client(request),
        zone_id=zone_id,
    )


# =============================================================================
# Exception Handling
# =============================================================================

def _register_pay_exception_handlers(app: Any) -> None:
    """Register centralized exception handlers for NexusPay errors.

    Call this after including the router in the app.
    """
    from nexus.pay.credits import (
        InsufficientCreditsError,
        ReservationError,
        WalletNotFoundError,
    )
    from nexus.pay.sdk import BudgetExceededError, NexusPayError

    exception_map: list[tuple[type, int, str]] = [
        (InsufficientCreditsError, 402, "insufficient_credits"),
        (WalletNotFoundError, 404, "wallet_not_found"),
        (BudgetExceededError, 403, "budget_exceeded"),
        (ReservationError, 409, "reservation_error"),
        (NexusPayError, 400, "pay_error"),
    ]

    # Register most specific exceptions first (subclasses before base)
    for exc_type, status_code, error_code in exception_map:

        async def _handler(
            request: Request,
            exc: Exception,
            _status: int = status_code,
            _code: str = error_code,
        ) -> JSONResponse:
            logger.warning(
                "NexusPay error",
                extra={"error": str(exc), "error_code": _code, "path": request.url.path},
            )
            return JSONResponse(
                status_code=_status,
                content={"detail": str(exc), "error_code": _code},
            )

        app.add_exception_handler(exc_type, _handler)


# =============================================================================
# Response Converters
# =============================================================================


def _receipt_to_response(receipt: Any) -> ReceiptResponse:
    """Convert SDK Receipt dataclass to Pydantic response."""
    return ReceiptResponse(
        id=receipt.id,
        method=receipt.method,
        amount=str(receipt.amount),
        from_agent=receipt.from_agent,
        to_agent=receipt.to_agent,
        memo=receipt.memo,
        timestamp=receipt.timestamp.isoformat() if receipt.timestamp else None,
        tx_hash=receipt.tx_hash,
    )


def _reservation_to_response(reservation: Any) -> ReservationResponse:
    """Convert SDK Reservation dataclass to Pydantic response."""
    return ReservationResponse(
        id=reservation.id,
        amount=str(reservation.amount),
        purpose=reservation.purpose,
        expires_at=reservation.expires_at.isoformat() if reservation.expires_at else None,
        status=reservation.status,
    )


# =============================================================================
# Endpoints
# =============================================================================


@router.get("/balance", response_model=BalanceResponse)
async def get_balance(
    nexuspay: Any = Depends(get_nexuspay),
) -> BalanceResponse:
    """Get agent's current balance.

    Returns available, reserved, and total balance.
    """
    balance = await nexuspay.get_balance()
    return BalanceResponse(
        available=str(balance.available),
        reserved=str(balance.reserved),
        total=str(balance.total),
    )


@router.get("/can-afford", response_model=CanAffordResponse)
async def can_afford(
    amount: str = Query(..., description="Amount to check as decimal string"),
    nexuspay: Any = Depends(get_nexuspay),
) -> CanAffordResponse:
    """Check if agent can afford a given amount.

    Note: This is a point-in-time check. For guaranteed atomicity,
    use the reserve endpoint instead.
    """
    # Validate the amount
    try:
        _validate_amount(amount)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    result = await nexuspay.can_afford(Decimal(amount))
    return CanAffordResponse(can_afford=result, amount=amount)


@router.post("/transfer", response_model=ReceiptResponse, status_code=201)
async def transfer(
    request: TransferRequestModel,
    nexuspay: Any = Depends(get_nexuspay),
) -> ReceiptResponse:
    """Transfer credits to another agent or external wallet.

    Auto-routes to credits (internal) or x402 (external wallet address).
    Override with 'method' field if needed.
    """
    receipt = await nexuspay.transfer(
        to=request.to,
        amount=Decimal(request.amount),
        memo=request.memo,
        idempotency_key=request.idempotency_key,
        method=request.method,
    )
    return _receipt_to_response(receipt)


@router.post("/transfer/batch", response_model=list[ReceiptResponse], status_code=201)
async def transfer_batch(
    request: BatchTransferRequestModel,
    nexuspay: Any = Depends(get_nexuspay),
) -> list[ReceiptResponse]:
    """Execute atomic batch transfer.

    All transfers succeed or all fail. Maximum 1000 transfers per batch.
    """
    from nexus.pay.credits import TransferRequest

    transfer_requests = [
        TransferRequest(
            from_id="",  # Overridden by SDK
            to_id=t.to,
            amount=Decimal(t.amount),
            memo=t.memo,
        )
        for t in request.transfers
    ]

    receipts = await nexuspay.transfer_batch(transfer_requests)
    return [_receipt_to_response(r) for r in receipts]


@router.post("/reserve", response_model=ReservationResponse, status_code=201)
async def reserve(
    request: ReserveRequestModel,
    nexuspay: Any = Depends(get_nexuspay),
) -> ReservationResponse:
    """Reserve credits for a pending operation.

    Creates a two-phase transfer. Reserved credits are held until
    committed or released (or auto-released after timeout).
    """
    reservation = await nexuspay.reserve(
        amount=Decimal(request.amount),
        timeout=request.timeout,
        purpose=request.purpose,
        task_id=request.task_id,
    )
    return _reservation_to_response(reservation)


@router.post("/reserve/{reservation_id}/commit", status_code=204)
async def commit_reservation(
    reservation_id: str,
    request: CommitRequestModel | None = None,
    nexuspay: Any = Depends(get_nexuspay),
) -> None:
    """Commit a reservation (charge actual amount).

    If actual_amount is provided and less than reserved, the difference
    is automatically refunded. If not provided, the full reserved amount
    is charged.
    """
    actual_amount = Decimal(request.actual_amount) if request and request.actual_amount else None
    await nexuspay.commit(reservation_id, actual_amount=actual_amount)


@router.post("/reserve/{reservation_id}/release", status_code=204)
async def release_reservation(
    reservation_id: str,
    nexuspay: Any = Depends(get_nexuspay),
) -> None:
    """Release a reservation (full refund).

    The reserved credits are returned to the agent's available balance.
    """
    await nexuspay.release(reservation_id)


@router.post("/meter", response_model=MeterResponse)
async def meter(
    request: MeterRequestModel,
    nexuspay: Any = Depends(get_nexuspay),
) -> MeterResponse:
    """Record metered usage (fast credit deduction).

    Returns success=true if deduction succeeded, false if insufficient credits.
    This is designed for high-throughput operations like API call metering.
    """
    success = await nexuspay.meter(
        amount=Decimal(request.amount),
        event_type=request.event_type,
    )
    return MeterResponse(success=success)


# =============================================================================
# Module Exports
# =============================================================================

__all__ = ["router", "_register_pay_exception_handlers"]
