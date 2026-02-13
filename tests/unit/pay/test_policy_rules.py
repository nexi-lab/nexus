"""Tests for Policy DSL/JSON Rules Engine (Phase 4).

Issue #1358: Tests rule evaluation against transaction context.

Rule types tested:
    1. recipient_allowlist
    2. recipient_blocklist
    3. time_window
    4. metadata_match
    5. amount_range
    6. Unknown rule types (skip gracefully)
    7. Multiple rules (first deny short-circuits)
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from nexus.pay.policy_rules import RuleContext, RuleResult, evaluate_rules


def _ctx(
    *,
    agent_id: str = "agent-a",
    zone_id: str = "default",
    to: str = "agent-b",
    amount: Decimal = Decimal("10"),
    metadata: dict | None = None,
    timestamp: datetime | None = None,
) -> RuleContext:
    kwargs: dict = {
        "agent_id": agent_id,
        "zone_id": zone_id,
        "to": to,
        "amount": amount,
        "metadata": metadata or {},
    }
    if timestamp is not None:
        kwargs["timestamp"] = timestamp
    return RuleContext(**kwargs)


class TestRecipientAllowlist:
    def test_allowed_recipient(self):
        rules = [{"type": "recipient_allowlist", "recipients": ["agent-b", "agent-c"]}]
        result = evaluate_rules(rules, _ctx(to="agent-b"))
        assert result.allowed is True

    def test_blocked_recipient(self):
        rules = [{"type": "recipient_allowlist", "recipients": ["agent-b"]}]
        result = evaluate_rules(rules, _ctx(to="agent-x"))
        assert result.allowed is False
        assert "not in allowlist" in result.denied_reason

    def test_empty_allowlist_allows_all(self):
        rules = [{"type": "recipient_allowlist", "recipients": []}]
        result = evaluate_rules(rules, _ctx(to="anyone"))
        assert result.allowed is True


class TestRecipientBlocklist:
    def test_blocked_recipient(self):
        rules = [{"type": "recipient_blocklist", "recipients": ["bad-agent"]}]
        result = evaluate_rules(rules, _ctx(to="bad-agent"))
        assert result.allowed is False
        assert "blocked" in result.denied_reason

    def test_allowed_recipient(self):
        rules = [{"type": "recipient_blocklist", "recipients": ["bad-agent"]}]
        result = evaluate_rules(rules, _ctx(to="good-agent"))
        assert result.allowed is True


class TestTimeWindow:
    def test_within_window(self):
        rules = [{"type": "time_window", "start_hour": 9, "end_hour": 17}]
        ts = datetime(2026, 2, 13, 12, 0, 0, tzinfo=UTC)  # noon
        result = evaluate_rules(rules, _ctx(timestamp=ts))
        assert result.allowed is True

    def test_outside_window(self):
        rules = [{"type": "time_window", "start_hour": 9, "end_hour": 17}]
        ts = datetime(2026, 2, 13, 20, 0, 0, tzinfo=UTC)  # 8pm
        result = evaluate_rules(rules, _ctx(timestamp=ts))
        assert result.allowed is False
        assert "outside allowed hours" in result.denied_reason

    def test_overnight_window_within(self):
        rules = [{"type": "time_window", "start_hour": 22, "end_hour": 6}]
        ts = datetime(2026, 2, 13, 23, 0, 0, tzinfo=UTC)  # 11pm
        result = evaluate_rules(rules, _ctx(timestamp=ts))
        assert result.allowed is True

    def test_overnight_window_outside(self):
        rules = [{"type": "time_window", "start_hour": 22, "end_hour": 6}]
        ts = datetime(2026, 2, 13, 12, 0, 0, tzinfo=UTC)  # noon
        result = evaluate_rules(rules, _ctx(timestamp=ts))
        assert result.allowed is False

    def test_at_start_hour_allowed(self):
        rules = [{"type": "time_window", "start_hour": 9, "end_hour": 17}]
        ts = datetime(2026, 2, 13, 9, 0, 0, tzinfo=UTC)
        result = evaluate_rules(rules, _ctx(timestamp=ts))
        assert result.allowed is True

    def test_at_end_hour_denied(self):
        rules = [{"type": "time_window", "start_hour": 9, "end_hour": 17}]
        ts = datetime(2026, 2, 13, 17, 0, 0, tzinfo=UTC)  # end_hour is exclusive
        result = evaluate_rules(rules, _ctx(timestamp=ts))
        assert result.allowed is False


class TestMetadataMatch:
    def test_matching_metadata(self):
        rules = [{"type": "metadata_match", "required": {"purpose": "salary"}}]
        result = evaluate_rules(rules, _ctx(metadata={"purpose": "salary"}))
        assert result.allowed is True

    def test_mismatched_metadata(self):
        rules = [{"type": "metadata_match", "required": {"purpose": "salary"}}]
        result = evaluate_rules(rules, _ctx(metadata={"purpose": "bonus"}))
        assert result.allowed is False
        assert "must be" in result.denied_reason

    def test_missing_metadata_key(self):
        rules = [{"type": "metadata_match", "required": {"purpose": "salary"}}]
        result = evaluate_rules(rules, _ctx(metadata={}))
        assert result.allowed is False


class TestAmountRange:
    def test_within_range(self):
        rules = [{"type": "amount_range", "min": "1", "max": "100"}]
        result = evaluate_rules(rules, _ctx(amount=Decimal("50")))
        assert result.allowed is True

    def test_below_min(self):
        rules = [{"type": "amount_range", "min": "10"}]
        result = evaluate_rules(rules, _ctx(amount=Decimal("5")))
        assert result.allowed is False
        assert "below minimum" in result.denied_reason

    def test_above_max(self):
        rules = [{"type": "amount_range", "max": "100"}]
        result = evaluate_rules(rules, _ctx(amount=Decimal("150")))
        assert result.allowed is False
        assert "above maximum" in result.denied_reason

    def test_at_boundaries(self):
        rules = [{"type": "amount_range", "min": "10", "max": "10"}]
        result = evaluate_rules(rules, _ctx(amount=Decimal("10")))
        assert result.allowed is True


class TestMultipleRules:
    def test_all_pass(self):
        rules = [
            {"type": "recipient_allowlist", "recipients": ["agent-b"]},
            {"type": "amount_range", "max": "100"},
        ]
        result = evaluate_rules(rules, _ctx(to="agent-b", amount=Decimal("50")))
        assert result.allowed is True

    def test_first_deny_short_circuits(self):
        rules = [
            {"type": "recipient_blocklist", "recipients": ["agent-b"]},
            {"type": "amount_range", "max": "100"},
        ]
        result = evaluate_rules(rules, _ctx(to="agent-b"))
        assert result.allowed is False
        assert result.matched_rule_type == "recipient_blocklist"

    def test_second_rule_denies(self):
        rules = [
            {"type": "recipient_allowlist", "recipients": ["agent-b"]},
            {"type": "amount_range", "max": "5"},
        ]
        result = evaluate_rules(rules, _ctx(to="agent-b", amount=Decimal("10")))
        assert result.allowed is False
        assert result.matched_rule_type == "amount_range"


class TestUnknownRuleType:
    def test_unknown_type_skipped(self):
        rules = [{"type": "unknown_rule_xyz"}]
        result = evaluate_rules(rules, _ctx())
        assert result.allowed is True

    def test_empty_rules(self):
        result = evaluate_rules([], _ctx())
        assert result.allowed is True


class TestRuleResult:
    def test_allowed_result(self):
        r = RuleResult(allowed=True)
        assert r.allowed is True
        assert r.denied_reason is None

    def test_denied_result(self):
        r = RuleResult(allowed=False, denied_reason="blocked", matched_rule_type="blocklist")
        assert r.allowed is False
        assert r.denied_reason == "blocked"
