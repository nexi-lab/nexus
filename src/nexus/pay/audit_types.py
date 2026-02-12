"""StrEnum definitions for exchange transaction audit logging.

Issue #1360 Phase 1: Transaction Audit Log types.
Stored as String columns (not PG ENUM) for forward-compatible schema evolution.
"""

from __future__ import annotations

from enum import StrEnum


class TransactionProtocol(StrEnum):
    """Payment protocol used for the transaction."""

    X402 = "x402"
    ACP = "acp"
    AP2 = "ap2"
    INTERNAL = "internal"


class TransactionStatus(StrEnum):
    """Lifecycle status of a transaction."""

    INITIATED = "initiated"
    SETTLED = "settled"
    FAILED = "failed"
    REFUNDED = "refunded"
    DISPUTED = "disputed"


class TransactionApplication(StrEnum):
    """Application domain that originated the transaction."""

    GATEWAY = "gateway"
    ADS = "ads"
    TOOLS = "tools"
    KNOWLEDGE = "knowledge"
    LABOR = "labor"
