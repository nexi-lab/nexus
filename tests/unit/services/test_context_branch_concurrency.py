"""Unit tests for ContextBranchService optimistic concurrency (Issue #1315, A3-B).

Tests:
- pointer_version increment on HEAD advance
- StalePointerError on concurrent update
- Retry with exponential backoff (P3-A)
- Retry exhaustion raises
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from nexus.contracts.exceptions import BranchNotFoundError, StalePointerError
from nexus.services.workspace.context_branch import (
    _BASE_BACKOFF_MS,
    _MAX_RETRIES,
    ContextBranchService,
)
from nexus.storage.models._base import Base
from nexus.storage.models.context_branch import ContextBranchModel


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine)


@pytest.fixture
def record_store(session_factory):
    return SimpleNamespace(session_factory=session_factory)


@pytest.fixture
def service(record_store):
    wm = MagicMock()
    wm.metadata = MagicMock()
    wm.backend = MagicMock()
    return ContextBranchService(
        workspace_manager=wm,
        record_store=record_store,
        rebac_manager=None,
        default_zone_id="z1",
    )


def _setup_branch(session_factory, workspace: str = "/ws", pointer_version: int = 0) -> str:
    """Create a main branch and return its ID."""
    with session_factory() as session:
        branch = ContextBranchModel(
            zone_id="z1",
            workspace_path=workspace,
            branch_name="main",
            head_snapshot_id="snap-old",
            is_current=True,
            status="active",
            pointer_version=pointer_version,
        )
        session.add(branch)
        session.commit()
        return branch.id


class TestPointerVersionIncrement:
    def test_advance_increments_version(self, service, session_factory):
        _setup_branch(session_factory, pointer_version=0)
        service._advance_head("z1", "/ws", "main", "snap-new")

        with session_factory() as session:
            branch = session.execute(
                select(ContextBranchModel).where(ContextBranchModel.branch_name == "main")
            ).scalar_one()
            assert branch.pointer_version == 1
            assert branch.head_snapshot_id == "snap-new"

    def test_multiple_advances_increment(self, service, session_factory):
        _setup_branch(session_factory, pointer_version=0)
        service._advance_head("z1", "/ws", "main", "snap-1")
        service._advance_head("z1", "/ws", "main", "snap-2")
        service._advance_head("z1", "/ws", "main", "snap-3")

        with session_factory() as session:
            branch = session.execute(
                select(ContextBranchModel).where(ContextBranchModel.branch_name == "main")
            ).scalar_one()
            assert branch.pointer_version == 3
            assert branch.head_snapshot_id == "snap-3"


class TestStalePointerDetection:
    def test_stale_pointer_raises(self, service, session_factory):
        """Simulate concurrent update by patching _advance_head to observe the race.

        We intercept the session to bump pointer_version BETWEEN the read and the CAS
        update, simulating another agent's concurrent commit.
        """
        _setup_branch(session_factory, pointer_version=0)

        # Monkey-patch: intercept the update to simulate concurrent modification
        call_count = 0

        original_session_factory = service._session_factory

        def racing_session_factory():
            """Session factory that bumps version between read and write."""
            session = original_session_factory()
            original_execute = session.execute

            def patched_execute(stmt, *args, **kwargs):
                nonlocal call_count
                result = original_execute(stmt, *args, **kwargs)
                call_count += 1
                # After the SELECT (call 1), before the UPDATE (call 2),
                # bump the version to simulate a race
                if call_count == 1:
                    # Use a separate session to bump the version
                    with original_session_factory() as s2:
                        branch = s2.execute(
                            select(ContextBranchModel).where(
                                ContextBranchModel.branch_name == "main"
                            )
                        ).scalar_one()
                        branch.pointer_version = 99
                        s2.commit()
                return result

            session.execute = patched_execute
            return session

        service._session_factory = racing_session_factory

        with pytest.raises(StalePointerError):
            service._advance_head("z1", "/ws", "main", "snap-new")

        # Restore
        service._session_factory = original_session_factory

    def test_advance_nonexistent_branch_raises(self, service, session_factory):
        with pytest.raises(BranchNotFoundError):
            service._advance_head("z1", "/ws", "ghost", "snap-new")


class TestRetryWithBackoff:
    @patch("nexus.services.workspace.context_branch.time.sleep")
    def test_retry_succeeds_on_second_attempt(self, mock_sleep, service, session_factory):
        _setup_branch(session_factory, pointer_version=0)

        call_count = 0
        original_advance = service._advance_head

        def flaky_advance(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise StalePointerError("main", 0, 1)
            return original_advance(*args, **kwargs)

        with patch.object(service, "_advance_head", side_effect=flaky_advance):
            service._advance_head_with_retry("z1", "/ws", "main", "snap-new")

        assert call_count == 2
        # First backoff: 10ms
        mock_sleep.assert_called_once_with(_BASE_BACKOFF_MS / 1000.0)

    @patch("nexus.services.workspace.context_branch.time.sleep")
    def test_retry_exhaustion_raises(self, mock_sleep, service, session_factory):
        _setup_branch(session_factory, pointer_version=0)

        with (
            patch.object(
                service,
                "_advance_head",
                side_effect=StalePointerError("main", 0, 1),
            ),
            pytest.raises(StalePointerError),
        ):
            service._advance_head_with_retry("z1", "/ws", "main", "snap-new")

        # Should have retried MAX_RETRIES - 1 times (last attempt doesn't sleep)
        assert mock_sleep.call_count == _MAX_RETRIES - 1

    @patch("nexus.services.workspace.context_branch.time.sleep")
    def test_exponential_backoff_timing(self, mock_sleep, service, session_factory):
        _setup_branch(session_factory, pointer_version=0)

        with (
            patch.object(
                service,
                "_advance_head",
                side_effect=StalePointerError("main", 0, 1),
            ),
            pytest.raises(StalePointerError),
        ):
            service._advance_head_with_retry("z1", "/ws", "main", "snap-new")

        # Verify exponential backoff: 10ms, 20ms
        calls = [c.args[0] for c in mock_sleep.call_args_list]
        assert calls == [0.01, 0.02]  # 10ms, 20ms
