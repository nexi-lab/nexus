"""Backward-compat shim: nexus.services.permissions.circuit_breaker.

Canonical location: ``nexus.rebac.circuit_breaker``
"""

from nexus.rebac.circuit_breaker import (
    INFRASTRUCTURE_EXCEPTIONS,
    AsyncCircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
)

__all__ = [
    "INFRASTRUCTURE_EXCEPTIONS",
    "AsyncCircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitState",
]
