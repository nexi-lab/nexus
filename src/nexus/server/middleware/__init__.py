"""Middleware modules for Nexus server.

This package contains middleware components for request processing:
- correlation: Request correlation ID generation and propagation (Issue #1002)
- x402: Payment verification middleware for HTTP 402 protocol
"""

from nexus.server.middleware.correlation import CorrelationMiddleware
from nexus.server.middleware.x402 import X402PaymentMiddleware, requires_payment

__all__ = ["CorrelationMiddleware", "X402PaymentMiddleware", "requires_payment"]
