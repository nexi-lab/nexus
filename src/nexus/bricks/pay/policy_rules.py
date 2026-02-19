"""Policy DSL/JSON Rules Engine (Phase 4).

Issue #1358: Evaluate declarative JSON rules against transaction context.

Supported rule types:
    - recipient_allowlist: Only allow transfers to specific recipients.
    - recipient_blocklist: Block transfers to specific recipients.
    - time_window: Only allow transfers during specific UTC hours.
    - metadata_match: Require specific key-value pairs in metadata.

Rules are evaluated in order. First matching deny rule short-circuits.
If all rules pass (or no rules), the transaction is allowed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

# Type alias for rule handler functions
_RuleHandler = Callable[[dict[str, Any], "RuleContext"], "RuleResult"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuleContext:
    """Context provided to the rule evaluator for each transaction."""

    agent_id: str
    zone_id: str
    to: str
    amount: Decimal
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class RuleResult:
    """Result of evaluating all rules."""

    allowed: bool
    denied_reason: str | None = None
    matched_rule_type: str | None = None


def evaluate_rules(rules: list[dict[str, Any]], context: RuleContext) -> RuleResult:
    """Evaluate a list of rules against the transaction context.

    Rules are checked in order. Each rule has a "type" and type-specific params.
    A failing rule immediately returns denied. All rules passing returns allowed.

    Args:
        rules: List of rule dicts, each with "type" and rule-specific keys.
        context: Transaction context to evaluate against.

    Returns:
        RuleResult with allowed=True if all rules pass.
    """
    for rule in rules:
        rule_type = rule.get("type", "")
        handler = _RULE_HANDLERS.get(rule_type)
        if handler is None:
            if logger.isEnabledFor(logging.WARNING):
                logger.warning("Unknown rule type: %s — skipping", rule_type)
            continue
        result = handler(rule, context)
        if not result.allowed:
            return result
    return RuleResult(allowed=True)


# =============================================================================
# Rule Handlers
# =============================================================================


def _eval_recipient_allowlist(rule: dict[str, Any], ctx: RuleContext) -> RuleResult:
    """Allow only if recipient is in the allowlist."""
    recipients: list[str] = rule.get("recipients", [])
    if not recipients:
        return RuleResult(allowed=True)
    if ctx.to in recipients:
        return RuleResult(allowed=True)
    return RuleResult(
        allowed=False,
        denied_reason=f"Recipient '{ctx.to}' not in allowlist",
        matched_rule_type="recipient_allowlist",
    )


def _eval_recipient_blocklist(rule: dict[str, Any], ctx: RuleContext) -> RuleResult:
    """Deny if recipient is in the blocklist."""
    recipients: list[str] = rule.get("recipients", [])
    if ctx.to in recipients:
        return RuleResult(
            allowed=False,
            denied_reason=f"Recipient '{ctx.to}' is blocked",
            matched_rule_type="recipient_blocklist",
        )
    return RuleResult(allowed=True)


def _eval_time_window(rule: dict[str, Any], ctx: RuleContext) -> RuleResult:
    """Allow only during specified UTC hours [start, end)."""
    start_hour: int = rule.get("start_hour", 0)
    end_hour: int = rule.get("end_hour", 24)
    current_hour = ctx.timestamp.hour

    if start_hour <= end_hour:
        # Normal range: e.g., 9-17
        allowed = start_hour <= current_hour < end_hour
    else:
        # Wrapping range: e.g., 22-6 (overnight)
        allowed = current_hour >= start_hour or current_hour < end_hour

    if allowed:
        return RuleResult(allowed=True)
    return RuleResult(
        allowed=False,
        denied_reason=(
            f"Transaction outside allowed hours ({start_hour}:00-{end_hour}:00 UTC, "
            f"current: {current_hour}:00 UTC)"
        ),
        matched_rule_type="time_window",
    )


def _eval_metadata_match(rule: dict[str, Any], ctx: RuleContext) -> RuleResult:
    """Require specific key-value pairs in transaction metadata."""
    required: dict[str, Any] = rule.get("required", {})
    for key, expected in required.items():
        actual = ctx.metadata.get(key)
        if actual != expected:
            return RuleResult(
                allowed=False,
                denied_reason=f"Metadata '{key}' must be '{expected}', got '{actual}'",
                matched_rule_type="metadata_match",
            )
    return RuleResult(allowed=True)


def _eval_amount_range(rule: dict[str, Any], ctx: RuleContext) -> RuleResult:
    """Allow only if amount is within [min, max] range."""
    min_amount = Decimal(str(rule["min"])) if "min" in rule else None
    max_amount = Decimal(str(rule["max"])) if "max" in rule else None

    if min_amount is not None and ctx.amount < min_amount:
        return RuleResult(
            allowed=False,
            denied_reason=f"Amount {ctx.amount} below minimum {min_amount}",
            matched_rule_type="amount_range",
        )
    if max_amount is not None and ctx.amount > max_amount:
        return RuleResult(
            allowed=False,
            denied_reason=f"Amount {ctx.amount} above maximum {max_amount}",
            matched_rule_type="amount_range",
        )
    return RuleResult(allowed=True)


# Registry of rule type → handler
_RULE_HANDLERS: dict[str, _RuleHandler] = {
    "recipient_allowlist": _eval_recipient_allowlist,
    "recipient_blocklist": _eval_recipient_blocklist,
    "time_window": _eval_time_window,
    "metadata_match": _eval_metadata_match,
    "amount_range": _eval_amount_range,
}
