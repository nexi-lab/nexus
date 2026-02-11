"""Unit tests for Close-to-Open Consistency Model (Issue #923).

Tests the FSConsistency enum, OperationContext consistency fields,
and the consistency helper logic.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from nexus.core.consistency import DEFAULT_CONSISTENCY, FSConsistency
from nexus.core.permissions import OperationContext


class TestFSConsistencyEnum:
    """Tests for FSConsistency enum values and defaults."""

    def test_enum_has_three_values(self):
        """FSConsistency should have exactly 3 levels."""
        values = list(FSConsistency)
        assert len(values) == 3

    def test_eventual_value(self):
        """EVENTUAL should have the string value 'eventual'."""
        assert FSConsistency.EVENTUAL == "eventual"
        assert FSConsistency.EVENTUAL.value == "eventual"

    def test_close_to_open_value(self):
        """CLOSE_TO_OPEN should have the string value 'close_to_open'."""
        assert FSConsistency.CLOSE_TO_OPEN == "close_to_open"
        assert FSConsistency.CLOSE_TO_OPEN.value == "close_to_open"

    def test_strong_value(self):
        """STRONG should have the string value 'strong'."""
        assert FSConsistency.STRONG == "strong"
        assert FSConsistency.STRONG.value == "strong"

    def test_default_is_close_to_open(self):
        """Default consistency should be CLOSE_TO_OPEN (matches JuiceFS)."""
        assert DEFAULT_CONSISTENCY == FSConsistency.CLOSE_TO_OPEN

    def test_enum_constructible_from_string(self):
        """FSConsistency should be constructible from string values."""
        assert FSConsistency("eventual") == FSConsistency.EVENTUAL
        assert FSConsistency("close_to_open") == FSConsistency.CLOSE_TO_OPEN
        assert FSConsistency("strong") == FSConsistency.STRONG

    def test_invalid_value_raises(self):
        """Invalid string should raise ValueError."""
        with pytest.raises(ValueError):
            FSConsistency("invalid")


class TestOperationContextConsistency:
    """Tests for OperationContext consistency and min_zookie fields."""

    def test_default_consistency_is_close_to_open(self):
        """OperationContext should default to CLOSE_TO_OPEN consistency."""
        ctx = OperationContext(user="alice", groups=[])
        assert ctx.consistency == FSConsistency.CLOSE_TO_OPEN

    def test_consistency_override(self):
        """OperationContext should accept explicit consistency level."""
        ctx = OperationContext(
            user="alice",
            groups=[],
            consistency=FSConsistency.STRONG,
        )
        assert ctx.consistency == FSConsistency.STRONG

    def test_consistency_eventual(self):
        """OperationContext should accept EVENTUAL consistency."""
        ctx = OperationContext(
            user="alice",
            groups=[],
            consistency=FSConsistency.EVENTUAL,
        )
        assert ctx.consistency == FSConsistency.EVENTUAL

    def test_min_zookie_default_is_none(self):
        """OperationContext.min_zookie should default to None."""
        ctx = OperationContext(user="alice", groups=[])
        assert ctx.min_zookie is None

    def test_min_zookie_can_be_set(self):
        """OperationContext should accept a min_zookie value."""
        from nexus.core.zookie import Zookie

        token = Zookie.encode("default", 42)
        ctx = OperationContext(
            user="alice",
            groups=[],
            min_zookie=token,
        )
        assert ctx.min_zookie == token

    def test_replace_preserves_consistency(self):
        """dataclasses.replace should preserve consistency when not overridden."""
        ctx = OperationContext(
            user="alice",
            groups=[],
            consistency=FSConsistency.STRONG,
        )
        new_ctx = replace(ctx, min_zookie="some_token")
        assert new_ctx.consistency == FSConsistency.STRONG
        assert new_ctx.min_zookie == "some_token"
        # Original should be unchanged (immutability)
        assert ctx.min_zookie is None

    def test_replace_overrides_consistency(self):
        """dataclasses.replace should override consistency."""
        ctx = OperationContext(user="alice", groups=[])
        new_ctx = replace(ctx, consistency=FSConsistency.EVENTUAL)
        assert new_ctx.consistency == FSConsistency.EVENTUAL
        # Original unchanged
        assert ctx.consistency == FSConsistency.CLOSE_TO_OPEN
