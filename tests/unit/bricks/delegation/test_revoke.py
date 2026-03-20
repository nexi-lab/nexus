"""Unit tests for DelegationService.revoke_delegation (Issue #2131, Phase 6.2).

Tests revocation lifecycle: happy path, double revoke, expired/completed
delegation, ReBAC failure propagation, and not-found error.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.bricks.delegation.errors import DelegationError, DelegationNotFoundError
from nexus.bricks.delegation.models import DelegationMode, DelegationStatus

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeDelegationRecord:
    """Minimal DelegationRecord stand-in."""

    delegation_id: str = "del-1"
    agent_id: str = "worker-1"
    parent_agent_id: str = "coordinator-1"
    delegation_mode: DelegationMode = DelegationMode.COPY
    status: DelegationStatus = DelegationStatus.ACTIVE
    scope_prefix: str | None = None
    lease_expires_at: datetime | None = None
    removed_grants: list[str] | None = None
    added_grants: list[str] | None = None
    readonly_paths: list[str] | None = None
    zone_id: str | None = "root"
    intent: str = ""
    parent_delegation_id: str | None = None
    depth: int = 0
    can_sub_delegate: bool = False
    scope: Any = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def _make_service(
    record: FakeDelegationRecord | None = None,
    rebac_raises: Exception | None = None,
) -> Any:
    """Create a DelegationService with mocked dependencies.

    Returns the service with:
        - _load_delegation_record: returns `record`
        - _update_delegation_status: no-op
        - _delete_worker_tuples: either no-op or raises `rebac_raises`
        - _revoke_worker_api_key: no-op
        - _agent_registry.unregister: no-op
    """
    from nexus.bricks.delegation.service import DelegationService

    mock_record_store = MagicMock()
    rebac_manager = MagicMock()
    agent_registry = MagicMock()

    service = DelegationService(
        record_store=mock_record_store,
        rebac_manager=rebac_manager,
        agent_registry=agent_registry,
    )

    # Patch internal methods
    service._load_delegation_record = MagicMock(return_value=record)
    service._update_delegation_status = MagicMock()
    service._revoke_worker_api_key = MagicMock()

    if rebac_raises is not None:
        service._delete_worker_tuples = MagicMock(side_effect=rebac_raises)
    else:
        service._delete_worker_tuples = MagicMock()

    return service


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRevokeHappyPath:
    """Test successful delegation revocation."""

    def test_revoke_happy_path(self) -> None:
        """Revoke an active delegation: status=REVOKED, grants deleted, key revoked, agent unregistered."""
        record = FakeDelegationRecord(status=DelegationStatus.ACTIVE)
        service = _make_service(record=record)

        result = service.revoke_delegation("del-1")

        assert result is True
        service._update_delegation_status.assert_called_once_with("del-1", DelegationStatus.REVOKED)
        service._delete_worker_tuples.assert_called_once_with("worker-1", "root")
        service._revoke_worker_api_key.assert_called_once_with("worker-1")
        service._agent_registry.unregister_external.assert_called_once_with("worker-1")


class TestRevokeAlreadyRevoked:
    """Test revoking an already-revoked delegation."""

    def test_revoke_double_raises(self) -> None:
        """Revoking an already-revoked delegation raises DelegationError."""
        record = FakeDelegationRecord(status=DelegationStatus.REVOKED)
        service = _make_service(record=record)

        with pytest.raises(DelegationError, match="not active"):
            service.revoke_delegation("del-1")

    def test_revoke_expired_raises(self) -> None:
        """Revoking an expired delegation raises DelegationError."""
        record = FakeDelegationRecord(status=DelegationStatus.EXPIRED)
        service = _make_service(record=record)

        with pytest.raises(DelegationError, match="not active"):
            service.revoke_delegation("del-1")

    def test_revoke_completed_raises(self) -> None:
        """Revoking a completed delegation raises DelegationError."""
        record = FakeDelegationRecord(status=DelegationStatus.COMPLETED)
        service = _make_service(record=record)

        with pytest.raises(DelegationError, match="not active"):
            service.revoke_delegation("del-1")


class TestRevokeRebackFailure:
    """Test ReBAC failure propagation during revocation."""

    def test_revoke_rebac_failure_propagates(self) -> None:
        """ReBAC deletion error propagates (fail-loud, Issue 7A)."""
        record = FakeDelegationRecord(status=DelegationStatus.ACTIVE)
        service = _make_service(
            record=record,
            rebac_raises=RuntimeError("ReBAC connection failed"),
        )

        with pytest.raises(RuntimeError, match="ReBAC connection failed"):
            service.revoke_delegation("del-1")

        # Status should have been set to REVOKED before the failure
        service._update_delegation_status.assert_called_once_with("del-1", DelegationStatus.REVOKED)


class TestRevokeNotFound:
    """Test revocation of non-existent delegation."""

    def test_revoke_not_found(self) -> None:
        """Revoking a non-existent delegation raises DelegationNotFoundError."""
        service = _make_service(record=None)

        with pytest.raises(DelegationNotFoundError):
            service.revoke_delegation("nonexistent")
