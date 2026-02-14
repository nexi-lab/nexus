"""Nexus Pay - Agent-to-agent payment system.

This module provides credit management for agent transactions using
TigerBeetle as the high-performance ledger backend.

Architecture:
    TigerBeetle handles: balances, transfers, reservations (40k+ TPS, <1ms)
    PostgreSQL handles: memos, metadata, budget settings, audit trails

Key Features:
    - Agent-to-agent credit transfers
    - Two-phase transfers (reserve/commit) with auto-timeout
    - Fast API metering / rate limiting
    - Batch transfers with atomic guarantees
    - Multi-zone support

Example:
    >>> from nexus.pay import CreditsService
    >>> service = CreditsService()
    >>> balance = await service.get_balance("agent-123")
    >>> await service.transfer("agent-a", "agent-b", Decimal("10"))

Related: Issue #1199, #1205, #1206, #1207
"""

from nexus.pay.constants import (
    ACCOUNT_CODE_ESCROW,
    ACCOUNT_CODE_TREASURY,
    ACCOUNT_CODE_WALLET,
    ESCROW_ACCOUNT_TB_ID,
    LEDGER_CREDITS,
    MICRO_UNIT_SCALE,
    SYSTEM_TREASURY_TB_ID,
    TRANSFER_CODE_API_USAGE,
    TRANSFER_CODE_PAYMENT,
    TRANSFER_CODE_PRIORITY_BID,
    TRANSFER_CODE_REFUND,
    TRANSFER_CODE_RESERVATION,
    TRANSFER_CODE_TOPUP,
    agent_id_to_tb_id,
    credits_to_micro,
    make_tb_account_id,
    micro_to_credits,
    zone_to_tb_prefix,
)
from nexus.pay.credits import (
    CreditsError,
    CreditsService,
    InsufficientCreditsError,
    ReservationError,
    TransferRequest,
    WalletNotFoundError,
)
from nexus.pay.policy_rules import RuleContext, RuleResult, evaluate_rules
from nexus.pay.policy_wrapper import PolicyEnforcedPayment
from nexus.pay.protocol import (
    CreditsPaymentProtocol,
    PaymentProtocol,
    ProtocolDetectionError,
    ProtocolDetector,
    ProtocolError,
    ProtocolNotFoundError,
    ProtocolRegistry,
    ProtocolTransferRequest,
    ProtocolTransferResult,
    X402PaymentProtocol,
    get_protocol_method_name,
)
from nexus.pay.sdk import (
    Balance,
    BudgetContext,
    BudgetExceededError,
    NexusPay,
    NexusPayError,
    Quote,
    Receipt,
    Reservation,
)
from nexus.pay.spending_policy import (
    ApprovalRequiredError,
    PolicyDeniedError,
    PolicyError,
    PolicyEvaluation,
    SpendingApproval,
    SpendingLedgerEntry,
    SpendingPolicy,
    SpendingRateLimitError,
)
from nexus.pay.spending_policy_service import SpendingPolicyService
from nexus.pay.x402 import (
    X402Client,
    X402Error,
    X402PaymentVerification,
    X402Receipt,
    micro_to_usdc,
    usdc_to_micro,
    validate_network,
    validate_wallet_address,
)

__all__ = [
    # Service
    "CreditsService",
    "TransferRequest",
    # Exceptions
    "CreditsError",
    "InsufficientCreditsError",
    "WalletNotFoundError",
    "ReservationError",
    # Constants
    "LEDGER_CREDITS",
    "ACCOUNT_CODE_WALLET",
    "ACCOUNT_CODE_ESCROW",
    "ACCOUNT_CODE_TREASURY",
    "TRANSFER_CODE_PAYMENT",
    "TRANSFER_CODE_TOPUP",
    "TRANSFER_CODE_RESERVATION",
    "TRANSFER_CODE_API_USAGE",
    "TRANSFER_CODE_PRIORITY_BID",
    "TRANSFER_CODE_REFUND",
    "SYSTEM_TREASURY_TB_ID",
    "ESCROW_ACCOUNT_TB_ID",
    "MICRO_UNIT_SCALE",
    # Utilities
    "agent_id_to_tb_id",
    "zone_to_tb_prefix",
    "make_tb_account_id",
    "credits_to_micro",
    "micro_to_credits",
    # x402 Protocol
    "X402Client",
    "X402Error",
    "X402Receipt",
    "X402PaymentVerification",
    "usdc_to_micro",
    "micro_to_usdc",
    "validate_wallet_address",
    "validate_network",
    # Protocol Abstraction Layer (#1357)
    "PaymentProtocol",
    "ProtocolRegistry",
    "ProtocolDetector",
    "ProtocolTransferRequest",
    "ProtocolTransferResult",
    "ProtocolError",
    "ProtocolNotFoundError",
    "ProtocolDetectionError",
    "X402PaymentProtocol",
    "CreditsPaymentProtocol",
    "get_protocol_method_name",
    # Unified SDK (#1207)
    "NexusPay",
    "NexusPayError",
    "BudgetExceededError",
    "Balance",
    "Receipt",
    "Reservation",
    "Quote",
    "BudgetContext",
    # Spending Policy Engine (#1358)
    "PolicyEnforcedPayment",
    "SpendingPolicyService",
    "SpendingPolicy",
    "SpendingLedgerEntry",
    "PolicyEvaluation",
    "PolicyError",
    "PolicyDeniedError",
    "ApprovalRequiredError",
    "SpendingRateLimitError",
    "SpendingApproval",
    # Policy Rules Engine (Phase 4: #1358)
    "RuleContext",
    "RuleResult",
    "evaluate_rules",
]
