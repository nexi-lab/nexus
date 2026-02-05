"""Nexus Pay - Agent-to-agent payment system.

This module provides credit management for agent transactions using
TigerBeetle as the high-performance ledger backend.

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
    tenant_to_tb_prefix,
)

__all__ = [
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
    "agent_id_to_tb_id",
    "tenant_to_tb_prefix",
    "make_tb_account_id",
    "credits_to_micro",
    "micro_to_credits",
]
