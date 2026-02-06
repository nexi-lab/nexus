"""x402 payment middleware for FastAPI.

This middleware intercepts requests to protected endpoints and verifies
x402 payment headers before allowing access.

Usage:
    app.add_middleware(
        X402PaymentMiddleware,
        x402_client=x402_client,
        protected_paths={
            "/api/v2/premium": Decimal("1.00"),
            "/api/v2/expensive": Decimal("5.00"),
        },
    )

Related: Issue #1206 (x402 protocol integration)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

if TYPE_CHECKING:
    from nexus.pay.x402 import X402Client

logger = logging.getLogger(__name__)


class X402PaymentMiddleware(BaseHTTPMiddleware):
    """Middleware for x402 payment verification on protected endpoints.

    This middleware:
    1. Checks if the request path requires payment
    2. If no X-Payment header, returns 402 with payment details
    3. If X-Payment header present, verifies payment with facilitator
    4. If payment valid, allows request to proceed
    5. If payment invalid, returns 402 with error

    Attributes:
        x402_client: X402Client instance for payment operations.
        protected_paths: Dict mapping path prefixes to prices in USDC.
        price_callback: Optional callback for dynamic pricing.
    """

    def __init__(
        self,
        app: Any,
        x402_client: X402Client,
        protected_paths: dict[str, Decimal] | None = None,
        price_callback: Callable[[Request], Decimal | None] | None = None,
    ):
        """Initialize middleware.

        Args:
            app: ASGI application.
            x402_client: X402Client for payment operations.
            protected_paths: Static path prefix to price mapping.
            price_callback: Dynamic pricing function (request -> price or None).
        """
        super().__init__(app)
        self.x402_client = x402_client
        self.protected_paths = protected_paths or {}
        self.price_callback = price_callback

    def _get_price_for_path(self, request: Request) -> Decimal | None:
        """Get price for a request path.

        Args:
            request: Incoming request.

        Returns:
            Price in USDC if path requires payment, None otherwise.
        """
        # Try dynamic pricing first
        if self.price_callback:
            price = self.price_callback(request)
            if price is not None:
                return price

        # Check static path mappings
        path = request.url.path
        for prefix, price in self.protected_paths.items():
            if path.startswith(prefix):
                return price

        return None

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request through middleware.

        Args:
            request: Incoming request.
            call_next: Next middleware/handler.

        Returns:
            Response from handler or 402 if payment required/invalid.
        """
        # Check if endpoint requires payment
        price = self._get_price_for_path(request)

        if price is None:
            # No payment required for this path
            response: Response = await call_next(request)
            return response

        # Check for payment header
        payment_header = request.headers.get("X-Payment")

        if not payment_header:
            # No payment provided, return 402 with payment details
            logger.debug(f"Payment required for {request.url.path}, amount={price}")
            return self.x402_client.payment_required_response(
                amount=price,
                description=f"Payment required for {request.url.path}",
            )

        # Verify payment
        verification = await self.x402_client.verify_payment(
            payment_header=payment_header,
            expected_amount=price,
        )

        if not verification.valid:
            logger.warning(
                f"Payment verification failed for {request.url.path}: {verification.error}"
            )
            return JSONResponse(
                status_code=402,
                content={
                    "error": "Payment verification failed",
                    "detail": verification.error,
                },
                headers={
                    "X-Payment-Error": verification.error or "Unknown error",
                },
            )

        # Payment valid, add receipt info to request state
        request.state.x402_payment = {
            "tx_hash": verification.tx_hash,
            "amount": str(verification.amount),
            "verified": True,
        }

        logger.info(
            f"Payment verified for {request.url.path}",
            extra={
                "tx_hash": verification.tx_hash,
                "amount": str(verification.amount),
            },
        )

        # Proceed with request
        response = await call_next(request)
        return response


def requires_payment(
    amount: Decimal,
    description: str = "API access",
) -> Callable:
    """Decorator to mark an endpoint as requiring x402 payment.

    This decorator can be used with FastAPI's dependency injection
    to protect individual endpoints.

    Usage:
        @router.get("/premium")
        @requires_payment(Decimal("1.00"))
        async def premium_endpoint():
            return {"data": "premium content"}

    Args:
        amount: Price in USDC.
        description: Payment description.

    Returns:
        Dependency that verifies payment or raises 402.
    """
    from fastapi import Depends

    async def verify_payment_dependency(request: Request) -> dict:
        """Dependency that verifies x402 payment."""
        x402_client = getattr(request.app.state, "x402_client", None)

        if not x402_client:
            # x402 not configured, allow request (for development)
            return {"payment_verified": False, "reason": "x402_not_configured"}

        payment_header = request.headers.get("X-Payment")

        if not payment_header:
            # No payment, return 402
            from fastapi import HTTPException

            raise HTTPException(
                status_code=402,
                detail={
                    "error": "Payment required",
                    "amount": str(amount),
                    "currency": "USDC",
                    "description": description,
                },
            )

        verification = await x402_client.verify_payment(
            payment_header=payment_header,
            expected_amount=amount,
        )

        if not verification.valid:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=402,
                detail={
                    "error": "Payment verification failed",
                    "reason": verification.error,
                },
            )

        return {
            "payment_verified": True,
            "tx_hash": verification.tx_hash,
            "amount": str(verification.amount),
        }

    def decorator(func: Callable) -> Callable:
        """Apply payment verification dependency."""
        import functools

        @functools.wraps(func)
        async def wrapper(
            *args: Any,
            payment_info: dict[str, Any] = Depends(verify_payment_dependency),
            **kwargs: Any,
        ) -> Any:
            # Add payment info to kwargs for handler access
            kwargs["payment_info"] = payment_info
            return await func(*args, **kwargs)

        return wrapper

    return decorator


__all__ = ["X402PaymentMiddleware", "requires_payment"]
