"""TigerBeetle constants for Nexus Pay.

These constants map to TigerBeetle's fixed schema fields:
- ledger: Identifies the ledger (namespace)
- code: Identifies account/transfer types
- flags: Control account/transfer behavior

Related: Issue #1199 (Nexus Pay hybrid architecture)
"""

from __future__ import annotations

import hashlib

# =============================================================================
# Ledger Codes
# =============================================================================
LEDGER_CREDITS = 1  # Main credits ledger

# =============================================================================
# Account Codes
# =============================================================================
ACCOUNT_CODE_WALLET = 1  # Agent wallet (normal account)
ACCOUNT_CODE_ESCROW = 2  # Escrow for pending transfers
ACCOUNT_CODE_TREASURY = 3  # System treasury

# =============================================================================
# Transfer Codes
# =============================================================================
TRANSFER_CODE_PAYMENT = 1  # Agent-to-agent payment
TRANSFER_CODE_TOPUP = 2  # Treasury -> Agent (credit purchase)
TRANSFER_CODE_RESERVATION = 3  # Two-phase reservation
TRANSFER_CODE_API_USAGE = 4  # API consumption charge
TRANSFER_CODE_PRIORITY_BID = 5  # Priority queue bid
TRANSFER_CODE_REFUND = 6  # Refund/reversal

# =============================================================================
# System Account IDs
# =============================================================================
SYSTEM_TREASURY_TB_ID = 1
ESCROW_ACCOUNT_TB_ID = 2

# =============================================================================
# Amount Conversion
# =============================================================================
# Credits are stored as integers in micro-units (6 decimal places)
# Example: 1.0 credit = 1_000_000 micro-credits
MICRO_UNIT_SCALE = 1_000_000


def credits_to_micro(credits: float) -> int:
    """Convert credits to micro-credits (internal storage format).

    Args:
        credits: Amount in credits (e.g., 1.5 credits)

    Returns:
        Amount in micro-credits (e.g., 1_500_000)
    """
    return int(credits * MICRO_UNIT_SCALE)


def micro_to_credits(micro: int) -> float:
    """Convert micro-credits to credits (display format).

    Args:
        micro: Amount in micro-credits

    Returns:
        Amount in credits
    """
    return micro / MICRO_UNIT_SCALE


# =============================================================================
# ID Conversion Utilities
# =============================================================================


def agent_id_to_tb_id(agent_id: str) -> int:
    """Convert string agent_id to TigerBeetle-compatible 64-bit integer."""
    hash_bytes = hashlib.md5(agent_id.encode()).digest()
    return int.from_bytes(hash_bytes[:8], byteorder="big") % (2**63)


def tenant_to_tb_prefix(tenant_id: str) -> int:
    """Convert tenant_id to upper 64-bit prefix for TigerBeetle ID."""
    hash_bytes = hashlib.md5(tenant_id.encode()).digest()
    return int.from_bytes(hash_bytes[8:16], byteorder="big") % (2**63)


def make_tb_account_id(tenant_id: str, agent_id: str) -> int:
    """Create full 128-bit TigerBeetle account ID."""
    upper = tenant_to_tb_prefix(tenant_id)
    lower = agent_id_to_tb_id(agent_id)
    return (upper << 64) | lower
