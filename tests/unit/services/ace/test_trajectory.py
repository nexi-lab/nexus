"""Unit tests for TrajectoryManager.

Tests start_trajectory, log_step, complete_trajectory,
get_trajectory, query_trajectories, and permission checks
using mocked database session and CAS backend.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.contracts.types import OperationContext, Permission
from nexus.services.ace.trajectory import TrajectoryManager


def _make_mock_backend() -> MagicMock:
    """Create a mock CAS backend that tracks writes and supports reads."""
    backend = MagicMock()
    stored: dict[str, bytes] = {}
    call_count = [0]

    def write_content(data: bytes) -> MagicMock:
        call_count[0] += 1
        key = f"hash-{call_count[0]}"
        stored[key] = data
        result = MagicMock()
        result.unwrap.return_value = key
        return result

    def read_content(hash_val: str) -> MagicMock:
        result = MagicMock()
        result.unwrap.return_value = stored.get(hash_val, b"{}")
        return result

    backend.write_content = write_content
    backend.read_content = read_content
    return backend


def _make_mock_session() -> MagicMock:
    """Create a mock SQLAlchemy session."""
    session = MagicMock()
    session.add = MagicMock()
    session.commit = MagicMock()
    return session


def _make_trajectory_model(**kwargs: Any) -> MagicMock:
    """Create a mock TrajectoryModel."""
    model = MagicMock()
    for key, val in kwargs.items():
        setattr(model, key, val)
    return model


# ---------------------------------------------------------------------------
# TrajectoryManager initialization
# ---------------------------------------------------------------------------


class TestTrajectoryManagerInit:
    """Tests for TrajectoryManager construction."""

    def test_basic_init(self) -> None:
        session = _make_mock_session()
        backend = _make_mock_backend()
        mgr = TrajectoryManager(session, backend, user_id="user-1")
        assert mgr.user_id == "user-1"
        assert mgr.agent_id is None
        assert mgr.zone_id is None
        assert mgr._active_trajectories == {}

    def test_init_with_all_params(self) -> None:
        session = _make_mock_session()
        backend = _make_mock_backend()
        ctx = OperationContext(user_id="user-1", groups=[], is_admin=True, is_system=False)
        mgr = TrajectoryManager(
            session,
            backend,
            user_id="user-1",
            agent_id="agent-1",
            zone_id="zone-1",
            context=ctx,
        )
        assert mgr.agent_id == "agent-1"
        assert mgr.zone_id == "zone-1"
        assert mgr.context.is_admin is True

    def test_default_context_created(self) -> None:
        session = _make_mock_session()
        backend = _make_mock_backend()
        mgr = TrajectoryManager(session, backend, user_id="user-1")
        assert mgr.context.user_id == "user-1"
        assert mgr.context.is_admin is False


# ---------------------------------------------------------------------------
# start_trajectory
# ---------------------------------------------------------------------------


class TestStartTrajectory:
    """Tests for start_trajectory."""

    def test_returns_trajectory_id(self) -> None:
        session = _make_mock_session()
        backend = _make_mock_backend()
        mgr = TrajectoryManager(session, backend, user_id="user-1")
        tid = mgr.start_trajectory("Test task")
        assert isinstance(tid, str)
        assert len(tid) > 0

    def test_stores_in_active_trajectories(self) -> None:
        session = _make_mock_session()
        backend = _make_mock_backend()
        mgr = TrajectoryManager(session, backend, user_id="user-1")
        tid = mgr.start_trajectory("Test task")
        assert tid in mgr._active_trajectories
        data = mgr._active_trajectories[tid]
        assert data["task_description"] == "Test task"

    def test_writes_trace_to_cas(self) -> None:
        session = _make_mock_session()
        backend = _make_mock_backend()
        mgr = TrajectoryManager(session, backend, user_id="user-1")
        tid = mgr.start_trajectory("Test task")
        # Backend write_content should have been called
        data = mgr._active_trajectories[tid]
        assert data["trace_hash"].startswith("hash-")

    def test_adds_model_to_session(self) -> None:
        session = _make_mock_session()
        backend = _make_mock_backend()
        mgr = TrajectoryManager(session, backend, user_id="user-1")
        mgr.start_trajectory("Test task")
        session.add.assert_called_once()
        session.commit.assert_called_once()

    def test_with_optional_params(self) -> None:
        session = _make_mock_session()
        backend = _make_mock_backend()
        mgr = TrajectoryManager(session, backend, user_id="user-1")
        tid = mgr.start_trajectory(
            "Test task",
            task_type="api_call",
            parent_trajectory_id="parent-1",
            metadata={"key": "val"},
            path="/project-a/",
        )
        data = mgr._active_trajectories[tid]
        assert data["task_type"] == "api_call"
        assert data["parent_trajectory_id"] == "parent-1"
        assert data["path"] == "/project-a/"


# ---------------------------------------------------------------------------
# log_step
# ---------------------------------------------------------------------------


class TestLogStep:
    """Tests for log_step."""

    def test_log_action_step(self) -> None:
        session = _make_mock_session()
        backend = _make_mock_backend()
        mgr = TrajectoryManager(session, backend, user_id="user-1")

        # Mock the DB query that log_step does to update trace_hash
        mock_result = MagicMock()
        mock_traj = MagicMock()
        mock_result.scalars.return_value.first.return_value = mock_traj
        session.execute.return_value = mock_result

        tid = mgr.start_trajectory("Test task")
        mgr.log_step(tid, "action", "Did something", result={"ok": True})

        trace = mgr._active_trajectories[tid]["trace"]
        assert len(trace["steps"]) == 1
        assert trace["steps"][0]["description"] == "Did something"

    def test_log_decision_step(self) -> None:
        session = _make_mock_session()
        backend = _make_mock_backend()
        mgr = TrajectoryManager(session, backend, user_id="user-1")

        mock_result = MagicMock()
        mock_traj = MagicMock()
        mock_result.scalars.return_value.first.return_value = mock_traj
        session.execute.return_value = mock_result

        tid = mgr.start_trajectory("Test task")
        mgr.log_step(tid, "decision", "Chose option A")

        trace = mgr._active_trajectories[tid]["trace"]
        assert len(trace["decisions"]) == 1

    def test_log_observation_step(self) -> None:
        session = _make_mock_session()
        backend = _make_mock_backend()
        mgr = TrajectoryManager(session, backend, user_id="user-1")

        mock_result = MagicMock()
        mock_traj = MagicMock()
        mock_result.scalars.return_value.first.return_value = mock_traj
        session.execute.return_value = mock_result

        tid = mgr.start_trajectory("Test task")
        mgr.log_step(tid, "observation", "Noticed something")

        trace = mgr._active_trajectories[tid]["trace"]
        assert len(trace["observations"]) == 1

    def test_log_step_unknown_trajectory_not_in_memory(self) -> None:
        session = _make_mock_session()
        backend = _make_mock_backend()
        mgr = TrajectoryManager(session, backend, user_id="user-1")

        # No trajectory in DB either
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = None
        session.execute.return_value = mock_result

        with pytest.raises(ValueError, match="not found"):
            mgr.log_step("nonexistent-id", "action", "test")


# ---------------------------------------------------------------------------
# _check_permission
# ---------------------------------------------------------------------------


class TestCheckPermission:
    """Tests for TrajectoryManager._check_permission."""

    def test_admin_bypass(self) -> None:
        session = _make_mock_session()
        backend = _make_mock_backend()
        ctx = OperationContext(user_id="admin", groups=[], is_admin=True, is_system=False)
        mgr = TrajectoryManager(session, backend, user_id="admin", context=ctx)

        model = MagicMock()
        model.agent_id = "other-agent"
        model.user_id = "other-user"
        assert mgr._check_permission(model, Permission.READ) is True

    def test_system_bypass(self) -> None:
        session = _make_mock_session()
        backend = _make_mock_backend()
        ctx = OperationContext(user_id="system", groups=[], is_admin=False, is_system=True)
        mgr = TrajectoryManager(session, backend, user_id="system", context=ctx)

        model = MagicMock()
        model.agent_id = "other-agent"
        model.user_id = "other-user"
        assert mgr._check_permission(model, Permission.READ) is True

    def test_creator_access(self) -> None:
        session = _make_mock_session()
        backend = _make_mock_backend()
        ctx = OperationContext(user_id="agent-1", groups=[], is_admin=False, is_system=False)
        mgr = TrajectoryManager(session, backend, user_id="agent-1", context=ctx)

        model = MagicMock()
        model.agent_id = "agent-1"  # Same as context user_id
        model.user_id = "other-user"
        assert mgr._check_permission(model, Permission.READ) is True

    def test_owner_access(self) -> None:
        session = _make_mock_session()
        backend = _make_mock_backend()
        ctx = OperationContext(user_id="user-1", groups=[], is_admin=False, is_system=False)
        mgr = TrajectoryManager(session, backend, user_id="user-1", context=ctx)

        model = MagicMock()
        model.agent_id = "agent-1"
        model.user_id = "user-1"  # Same as context user_id
        assert mgr._check_permission(model, Permission.READ) is True

    def test_denied_no_relation(self) -> None:
        session = _make_mock_session()
        backend = _make_mock_backend()
        ctx = OperationContext(user_id="user-1", groups=[], is_admin=False, is_system=False)
        mgr = TrajectoryManager(session, backend, user_id="user-1", context=ctx)

        model = MagicMock()
        model.agent_id = "agent-other"
        model.user_id = "user-other"
        assert mgr._check_permission(model, Permission.READ) is False
