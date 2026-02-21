"""Unit tests for delegation domain models (Issue #1271, #1618).

Tests DelegationMode, DelegationStatus, DelegationScope, DelegationRecord,
and DelegationResult frozen dataclasses.
"""

import dataclasses
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from nexus.services.delegation.models import (
    DelegationMode,
    DelegationRecord,
    DelegationResult,
    DelegationScope,
    DelegationStatus,
)

# ---------------------------------------------------------------------------
# DelegationMode enum
# ---------------------------------------------------------------------------


class TestDelegationMode:
    def test_values(self):
        assert DelegationMode.COPY.value == "copy"
        assert DelegationMode.CLEAN.value == "clean"
        assert DelegationMode.SHARED.value == "shared"

    def test_from_string(self):
        assert DelegationMode("copy") == DelegationMode.COPY
        assert DelegationMode("clean") == DelegationMode.CLEAN
        assert DelegationMode("shared") == DelegationMode.SHARED

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            DelegationMode("invalid")

    def test_all_modes(self):
        assert len(DelegationMode) == 3


# ---------------------------------------------------------------------------
# DelegationStatus enum (#1618)
# ---------------------------------------------------------------------------


class TestDelegationStatus:
    def test_values(self):
        assert DelegationStatus.ACTIVE.value == "active"
        assert DelegationStatus.REVOKED.value == "revoked"
        assert DelegationStatus.EXPIRED.value == "expired"
        assert DelegationStatus.COMPLETED.value == "completed"

    def test_from_string(self):
        assert DelegationStatus("active") == DelegationStatus.ACTIVE
        assert DelegationStatus("revoked") == DelegationStatus.REVOKED

    def test_all_statuses(self):
        assert len(DelegationStatus) == 4


# ---------------------------------------------------------------------------
# DelegationScope frozen dataclass (#1618)
# ---------------------------------------------------------------------------


class TestDelegationScope:
    def test_defaults(self):
        scope = DelegationScope()
        assert scope.allowed_operations == frozenset()
        assert scope.resource_patterns == frozenset()
        assert scope.budget_limit is None
        assert scope.max_depth == 0

    def test_with_values(self):
        scope = DelegationScope(
            allowed_operations=frozenset({"read", "write"}),
            resource_patterns=frozenset({"*.py"}),
            budget_limit=Decimal("100.50"),
            max_depth=3,
        )
        assert "read" in scope.allowed_operations
        assert scope.budget_limit == Decimal("100.50")
        assert scope.max_depth == 3

    def test_frozen(self):
        scope = DelegationScope()
        with pytest.raises(dataclasses.FrozenInstanceError):
            scope.max_depth = 5  # type: ignore[misc]

    def test_equality(self):
        s1 = DelegationScope(allowed_operations=frozenset({"read"}))
        s2 = DelegationScope(allowed_operations=frozenset({"read"}))
        assert s1 == s2

    def test_inequality(self):
        s1 = DelegationScope(max_depth=1)
        s2 = DelegationScope(max_depth=2)
        assert s1 != s2


# ---------------------------------------------------------------------------
# DelegationRecord frozen dataclass
# ---------------------------------------------------------------------------


class TestDelegationRecord:
    def test_required_fields(self):
        record = DelegationRecord(
            delegation_id="d1",
            agent_id="worker",
            parent_agent_id="coordinator",
            delegation_mode=DelegationMode.COPY,
        )
        assert record.delegation_id == "d1"
        assert record.agent_id == "worker"
        assert record.parent_agent_id == "coordinator"
        assert record.delegation_mode == DelegationMode.COPY

    def test_defaults(self):
        record = DelegationRecord(
            delegation_id="d1",
            agent_id="worker",
            parent_agent_id="coordinator",
            delegation_mode=DelegationMode.COPY,
        )
        assert record.status == DelegationStatus.ACTIVE
        assert record.scope_prefix is None
        assert record.scope is None
        assert record.lease_expires_at is None
        assert record.removed_grants == ()
        assert record.added_grants == ()
        assert record.readonly_paths == ()
        assert record.zone_id is None
        assert record.intent == ""
        assert record.parent_delegation_id is None
        assert record.depth == 0
        assert record.can_sub_delegate is False
        assert record.created_at is None

    def test_frozen(self):
        record = DelegationRecord(
            delegation_id="d1",
            agent_id="worker",
            parent_agent_id="coordinator",
            delegation_mode=DelegationMode.COPY,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            record.status = DelegationStatus.REVOKED  # type: ignore[misc]

    def test_with_1618_fields(self):
        now = datetime.now(UTC)
        scope = DelegationScope(max_depth=2)
        record = DelegationRecord(
            delegation_id="d2",
            agent_id="worker",
            parent_agent_id="coordinator",
            delegation_mode=DelegationMode.CLEAN,
            status=DelegationStatus.REVOKED,
            scope=scope,
            intent="Run tests",
            parent_delegation_id="d1",
            depth=1,
            can_sub_delegate=True,
            created_at=now,
        )
        assert record.status == DelegationStatus.REVOKED
        assert record.scope == scope
        assert record.intent == "Run tests"
        assert record.parent_delegation_id == "d1"
        assert record.depth == 1
        assert record.can_sub_delegate is True
        assert record.created_at == now

    def test_tuple_grants(self):
        record = DelegationRecord(
            delegation_id="d1",
            agent_id="worker",
            parent_agent_id="coordinator",
            delegation_mode=DelegationMode.COPY,
            removed_grants=("/a.py", "/b.py"),
            added_grants=("/c.py",),
            readonly_paths=("/d.py",),
        )
        assert isinstance(record.removed_grants, tuple)
        assert len(record.removed_grants) == 2

    def test_equality(self):
        kwargs = {
            "delegation_id": "d1",
            "agent_id": "worker",
            "parent_agent_id": "coordinator",
            "delegation_mode": DelegationMode.COPY,
        }
        assert DelegationRecord(**kwargs) == DelegationRecord(**kwargs)


# ---------------------------------------------------------------------------
# DelegationResult frozen dataclass
# ---------------------------------------------------------------------------


class TestDelegationResult:
    def test_fields(self):
        now = datetime.now(UTC)
        result = DelegationResult(
            delegation_id="d1",
            worker_agent_id="worker",
            api_key="sk-test-key",
            mount_table=["/workspace"],
            expires_at=now,
            delegation_mode=DelegationMode.COPY,
        )
        assert result.delegation_id == "d1"
        assert result.worker_agent_id == "worker"
        assert result.api_key == "sk-test-key"
        assert result.mount_table == ["/workspace"]
        assert result.expires_at == now
        assert result.delegation_mode == DelegationMode.COPY

    def test_frozen(self):
        result = DelegationResult(
            delegation_id="d1",
            worker_agent_id="worker",
            api_key="sk-test",
            mount_table=[],
            expires_at=None,
            delegation_mode=DelegationMode.COPY,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.api_key = "new"  # type: ignore[misc]
