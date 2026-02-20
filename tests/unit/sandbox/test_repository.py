"""Unit tests for SandboxRepository (Issue #2051).

Tests DB operations in isolation using an in-memory SQLite database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nexus.bricks.sandbox.repository import SandboxRepository
from nexus.bricks.sandbox.sandbox_provider import SandboxNotFoundError
from nexus.storage.models import SandboxMetadataModel
from tests.helpers.in_memory_record_store import InMemoryRecordStore


@pytest.fixture()
def record_store():
    """Create an InMemoryRecordStore for sandbox tests."""
    store = InMemoryRecordStore()
    yield store
    store.close()


@pytest.fixture()
def session_factory(record_store):
    """Create a session factory bound to the engine."""
    return record_store.session_factory


@pytest.fixture()
def repo(record_store):
    """Create a SandboxRepository instance."""
    return SandboxRepository(record_store=record_store)


@pytest.fixture()
def active_sandbox(session_factory) -> dict[str, str]:
    """Insert an active sandbox record and return its key fields."""
    session = session_factory()
    now = datetime.now(UTC)
    metadata = SandboxMetadataModel(
        sandbox_id="sb-001",
        name="test-sandbox",
        user_id="user-1",
        agent_id="agent-1",
        zone_id="zone-1",
        provider="docker",
        template_id=None,
        status="active",
        created_at=now,
        last_active_at=now,
        ttl_minutes=10,
        expires_at=now + timedelta(minutes=10),
        auto_created=1,
    )
    session.add(metadata)
    session.commit()
    session.close()
    return {
        "sandbox_id": "sb-001",
        "name": "test-sandbox",
        "user_id": "user-1",
        "agent_id": "agent-1",
        "zone_id": "zone-1",
        "provider": "docker",
    }


class TestGetMetadata:
    """Tests for SandboxRepository.get_metadata."""

    def test_returns_dict_for_existing_sandbox(self, repo, active_sandbox):
        result = repo.get_metadata(active_sandbox["sandbox_id"])

        assert result["sandbox_id"] == "sb-001"
        assert result["name"] == "test-sandbox"
        assert result["user_id"] == "user-1"
        assert result["status"] == "active"
        assert result["provider"] == "docker"
        assert isinstance(result["created_at"], str)
        assert isinstance(result["uptime_seconds"], float)

    def test_raises_not_found_for_missing_sandbox(self, repo):
        with pytest.raises(SandboxNotFoundError, match="not found"):
            repo.get_metadata("nonexistent-id")


class TestGetMetadataField:
    """Tests for SandboxRepository.get_metadata_field."""

    def test_returns_single_field(self, repo, active_sandbox):
        result = repo.get_metadata_field(active_sandbox["sandbox_id"], "provider")
        assert result == "docker"

    def test_returns_none_for_nullable_field(self, repo, active_sandbox):
        result = repo.get_metadata_field(active_sandbox["sandbox_id"], "template_id")
        assert result is None

    def test_raises_not_found_for_missing_sandbox(self, repo):
        with pytest.raises(SandboxNotFoundError, match="not found"):
            repo.get_metadata_field("nonexistent-id", "provider")


class TestUpdateMetadata:
    """Tests for SandboxRepository.update_metadata."""

    def test_updates_single_field(self, repo, active_sandbox):
        result = repo.update_metadata(active_sandbox["sandbox_id"], status="paused")

        assert result["status"] == "paused"
        assert result["sandbox_id"] == "sb-001"

    def test_updates_multiple_fields(self, repo, active_sandbox):
        now = datetime.now(UTC)
        result = repo.update_metadata(
            active_sandbox["sandbox_id"],
            status="stopped",
            stopped_at=now,
            expires_at=None,
        )

        assert result["status"] == "stopped"
        assert result["stopped_at"] is not None
        assert result["expires_at"] is None

    def test_raises_not_found_for_missing_sandbox(self, repo):
        with pytest.raises(SandboxNotFoundError, match="not found"):
            repo.update_metadata("nonexistent-id", status="stopped")


class TestCreateMetadata:
    """Tests for SandboxRepository.create_metadata."""

    def test_creates_new_sandbox_record(self, repo):
        now = datetime.now(UTC)
        result = repo.create_metadata(
            sandbox_id="sb-new",
            name="new-sandbox",
            user_id="user-1",
            zone_id="zone-1",
            agent_id=None,
            provider="docker",
            template_id=None,
            created_at=now,
            last_active_at=now,
            ttl_minutes=10,
            expires_at=now + timedelta(minutes=10),
        )

        assert result["sandbox_id"] == "sb-new"
        assert result["name"] == "new-sandbox"
        assert result["status"] == "active"

    def test_created_record_is_retrievable(self, repo):
        now = datetime.now(UTC)
        repo.create_metadata(
            sandbox_id="sb-retrieve",
            name="retrievable",
            user_id="user-1",
            zone_id="zone-1",
            agent_id=None,
            provider="docker",
            template_id=None,
            created_at=now,
            last_active_at=now,
            ttl_minutes=5,
            expires_at=now + timedelta(minutes=5),
        )

        result = repo.get_metadata("sb-retrieve")
        assert result["name"] == "retrievable"


class TestFindActiveSandbox:
    """Tests for SandboxRepository.find_active_by_name."""

    def test_finds_active_sandbox_by_name(self, repo, active_sandbox):
        result = repo.find_active_by_name(user_id="user-1", name="test-sandbox")

        assert result is not None
        assert result["sandbox_id"] == "sb-001"
        assert result["status"] == "active"

    def test_returns_none_when_no_match(self, repo, active_sandbox):
        result = repo.find_active_by_name(user_id="user-1", name="nonexistent")
        assert result is None

    def test_returns_none_for_stopped_sandbox(self, repo, active_sandbox):
        repo.update_metadata("sb-001", status="stopped")

        result = repo.find_active_by_name(user_id="user-1", name="test-sandbox")
        assert result is None


class TestListSandboxes:
    """Tests for SandboxRepository.list_sandboxes."""

    def test_lists_all_sandboxes(self, repo, active_sandbox):
        result = repo.list_sandboxes()
        assert len(result) == 1
        assert result[0]["sandbox_id"] == "sb-001"

    def test_filters_by_user_id(self, repo, active_sandbox):
        result = repo.list_sandboxes(user_id="user-1")
        assert len(result) == 1

        result = repo.list_sandboxes(user_id="other-user")
        assert len(result) == 0

    def test_filters_by_status(self, repo, active_sandbox):
        result = repo.list_sandboxes(status="active")
        assert len(result) == 1

        result = repo.list_sandboxes(status="stopped")
        assert len(result) == 0

    def test_filters_by_zone_id(self, repo, active_sandbox):
        result = repo.list_sandboxes(zone_id="zone-1")
        assert len(result) == 1

        result = repo.list_sandboxes(zone_id="other-zone")
        assert len(result) == 0

    def test_filters_by_agent_id(self, repo, active_sandbox):
        result = repo.list_sandboxes(agent_id="agent-1")
        assert len(result) == 1


class TestFindExpired:
    """Tests for SandboxRepository.find_expired."""

    def test_finds_expired_active_sandboxes(self, repo, session_factory):
        session = session_factory()
        now = datetime.now(UTC)
        # Create an expired sandbox
        metadata = SandboxMetadataModel(
            sandbox_id="sb-expired",
            name="expired",
            user_id="user-1",
            zone_id="zone-1",
            provider="docker",
            status="active",
            created_at=now - timedelta(minutes=20),
            last_active_at=now - timedelta(minutes=15),
            ttl_minutes=10,
            expires_at=now - timedelta(minutes=5),
            auto_created=1,
        )
        session.add(metadata)
        session.commit()
        session.close()

        result = repo.find_expired()
        assert "sb-expired" in result

    def test_excludes_non_active_sandboxes(self, repo, session_factory):
        session = session_factory()
        now = datetime.now(UTC)
        metadata = SandboxMetadataModel(
            sandbox_id="sb-stopped-expired",
            name="stopped-expired",
            user_id="user-1",
            zone_id="zone-1",
            provider="docker",
            status="stopped",
            created_at=now - timedelta(minutes=20),
            last_active_at=now - timedelta(minutes=15),
            ttl_minutes=10,
            expires_at=now - timedelta(minutes=5),
            auto_created=1,
        )
        session.add(metadata)
        session.commit()
        session.close()

        result = repo.find_expired()
        assert "sb-stopped-expired" not in result


class TestRetryBehavior:
    """Tests for database retry logic (Issue #2051 #12A)."""

    def test_succeeds_on_first_try(self, repo, active_sandbox):
        # Normal operation should work without retry
        result = repo.get_metadata("sb-001")
        assert result["sandbox_id"] == "sb-001"

    def test_retries_on_pending_rollback_error(self, record_store, active_sandbox):
        """PendingRollbackError on first try triggers retry that succeeds."""
        from contextlib import contextmanager
        from unittest.mock import patch

        from sqlalchemy.exc import PendingRollbackError

        repo = SandboxRepository(record_store=record_store)

        call_count = {"n": 0}
        original_get_session = repo._get_session

        @contextmanager
        def _flaky_session():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise PendingRollbackError("simulated rollback")
            with original_get_session() as session:
                yield session

        with patch.object(repo, "_get_session", side_effect=_flaky_session):
            result = repo.get_metadata("sb-001")

        assert result["sandbox_id"] == "sb-001"
        assert call_count["n"] == 2  # First failed, second succeeded

    def test_both_tries_fail_propagates_error(self, record_store, active_sandbox):
        """SQLAlchemyError on both tries propagates the second error."""
        from contextlib import contextmanager
        from unittest.mock import patch

        from sqlalchemy.exc import SQLAlchemyError

        repo = SandboxRepository(record_store=record_store)

        @contextmanager
        def _always_fail():
            raise SQLAlchemyError("database down")

        with (
            patch.object(repo, "_get_session", side_effect=_always_fail),
            pytest.raises(SQLAlchemyError, match="database down"),
        ):
            repo.get_metadata("sb-001")

    def test_find_expired_returns_empty_on_db_failure(self, record_store):
        """find_expired returns empty list on database failure (graceful degradation)."""
        from contextlib import contextmanager
        from unittest.mock import patch

        from sqlalchemy.exc import SQLAlchemyError

        repo = SandboxRepository(record_store=record_store)

        @contextmanager
        def _always_fail():
            raise SQLAlchemyError("connection lost")

        with patch.object(repo, "_get_session", side_effect=_always_fail):
            result = repo.find_expired()

        assert result == []
