"""Tests for background tasks."""

import asyncio
import contextlib
from datetime import timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest

from nexus.server.background_tasks import (
    inactive_session_cleanup_task,
    sandbox_cleanup_task,
    session_cleanup_task,
    start_background_tasks,
)


class TestSandboxCleanupTask:
    """Tests for sandbox_cleanup_task."""

    @pytest.mark.asyncio
    async def test_cleanup_expired_sandboxes_success(self):
        """Test successful sandbox cleanup."""
        # Mock sandbox manager
        sandbox_manager = Mock()
        sandbox_manager.cleanup_expired_sandboxes = AsyncMock(return_value=3)

        # Run task for one iteration
        task = asyncio.create_task(sandbox_cleanup_task(sandbox_manager, interval_seconds=0.1))

        # Let it run once
        await asyncio.sleep(0.2)
        task.cancel()

        with contextlib.suppress(asyncio.CancelledError):
            await task

        # Verify cleanup was called
        assert sandbox_manager.cleanup_expired_sandboxes.call_count >= 1

    @pytest.mark.asyncio
    async def test_cleanup_no_expired_sandboxes(self):
        """Test cleanup when no sandboxes expired."""
        sandbox_manager = Mock()
        sandbox_manager.cleanup_expired_sandboxes = AsyncMock(return_value=0)

        task = asyncio.create_task(sandbox_cleanup_task(sandbox_manager, interval_seconds=0.1))
        await asyncio.sleep(0.2)
        task.cancel()

        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert sandbox_manager.cleanup_expired_sandboxes.call_count >= 1

    @pytest.mark.asyncio
    async def test_cleanup_handles_exception(self):
        """Test that cleanup continues after exception."""
        sandbox_manager = Mock()
        sandbox_manager.cleanup_expired_sandboxes = AsyncMock(
            side_effect=[Exception("Test error"), 2]
        )

        task = asyncio.create_task(sandbox_cleanup_task(sandbox_manager, interval_seconds=0.1))
        await asyncio.sleep(0.3)
        task.cancel()

        with contextlib.suppress(asyncio.CancelledError):
            await task

        # Should have been called multiple times despite exception
        assert sandbox_manager.cleanup_expired_sandboxes.call_count >= 2


class TestSessionCleanupTask:
    """Tests for session_cleanup_task."""

    @pytest.mark.asyncio
    async def test_cleanup_expired_sessions_success(self):
        """Test successful session cleanup."""
        # Mock session factory
        mock_db = Mock()
        session_factory = Mock(return_value=mock_db)
        mock_db.__enter__ = Mock(return_value=mock_db)
        mock_db.__exit__ = Mock(return_value=None)

        with patch("nexus.server.background_tasks.cleanup_expired_sessions") as mock_cleanup:
            mock_cleanup.return_value = {"sessions": 5, "resources": 10}

            task = asyncio.create_task(session_cleanup_task(session_factory, interval_seconds=0.1))
            await asyncio.sleep(0.2)
            task.cancel()

            with contextlib.suppress(asyncio.CancelledError):
                await task

            # Verify cleanup was called
            assert mock_cleanup.call_count >= 1
            assert mock_db.commit.call_count >= 1

    @pytest.mark.asyncio
    async def test_cleanup_no_expired_sessions(self):
        """Test cleanup when no sessions expired."""
        mock_db = Mock()
        session_factory = Mock(return_value=mock_db)
        mock_db.__enter__ = Mock(return_value=mock_db)
        mock_db.__exit__ = Mock(return_value=None)

        with patch("nexus.server.background_tasks.cleanup_expired_sessions") as mock_cleanup:
            mock_cleanup.return_value = {"sessions": 0, "resources": 0}

            task = asyncio.create_task(session_cleanup_task(session_factory, interval_seconds=0.1))
            await asyncio.sleep(0.2)
            task.cancel()

            with contextlib.suppress(asyncio.CancelledError):
                await task

            assert mock_cleanup.call_count >= 1

    @pytest.mark.asyncio
    async def test_cleanup_handles_exception(self):
        """Test that cleanup continues after exception."""
        mock_db = Mock()
        session_factory = Mock(return_value=mock_db)
        mock_db.__enter__ = Mock(return_value=mock_db)
        mock_db.__exit__ = Mock(return_value=None)

        with patch("nexus.server.background_tasks.cleanup_expired_sessions") as mock_cleanup:
            mock_cleanup.side_effect = [Exception("Test error"), {"sessions": 3, "resources": 5}]

            task = asyncio.create_task(session_cleanup_task(session_factory, interval_seconds=0.1))
            await asyncio.sleep(0.3)
            task.cancel()

            with contextlib.suppress(asyncio.CancelledError):
                await task

            # Should continue despite exception
            assert mock_cleanup.call_count >= 2


class TestInactiveSessionCleanupTask:
    """Tests for inactive_session_cleanup_task."""

    @pytest.mark.asyncio
    async def test_cleanup_inactive_sessions_success(self):
        """Test successful inactive session cleanup."""
        mock_db = Mock()
        session_factory = Mock(return_value=mock_db)
        mock_db.__enter__ = Mock(return_value=mock_db)
        mock_db.__exit__ = Mock(return_value=None)

        with patch("nexus.server.background_tasks.cleanup_inactive_sessions") as mock_cleanup:
            mock_cleanup.return_value = 7

            task = asyncio.create_task(
                inactive_session_cleanup_task(
                    session_factory,
                    inactive_threshold=timedelta(days=30),
                    interval_seconds=0.1,
                )
            )
            await asyncio.sleep(0.2)
            task.cancel()

            with contextlib.suppress(asyncio.CancelledError):
                await task

            assert mock_cleanup.call_count >= 1
            assert mock_db.commit.call_count >= 1

    @pytest.mark.asyncio
    async def test_cleanup_no_inactive_sessions(self):
        """Test cleanup when no inactive sessions."""
        mock_db = Mock()
        session_factory = Mock(return_value=mock_db)
        mock_db.__enter__ = Mock(return_value=mock_db)
        mock_db.__exit__ = Mock(return_value=None)

        with patch("nexus.server.background_tasks.cleanup_inactive_sessions") as mock_cleanup:
            mock_cleanup.return_value = 0

            task = asyncio.create_task(
                inactive_session_cleanup_task(
                    session_factory, inactive_threshold=timedelta(days=7), interval_seconds=0.1
                )
            )
            await asyncio.sleep(0.2)
            task.cancel()

            with contextlib.suppress(asyncio.CancelledError):
                await task

            assert mock_cleanup.call_count >= 1

    @pytest.mark.asyncio
    async def test_cleanup_handles_exception(self):
        """Test that cleanup continues after exception."""
        mock_db = Mock()
        session_factory = Mock(return_value=mock_db)
        mock_db.__enter__ = Mock(return_value=mock_db)
        mock_db.__exit__ = Mock(return_value=None)

        with patch("nexus.server.background_tasks.cleanup_inactive_sessions") as mock_cleanup:
            mock_cleanup.side_effect = [Exception("Test error"), 4]

            task = asyncio.create_task(
                inactive_session_cleanup_task(
                    session_factory, inactive_threshold=timedelta(days=15), interval_seconds=0.1
                )
            )
            await asyncio.sleep(0.3)
            task.cancel()

            with contextlib.suppress(asyncio.CancelledError):
                await task

            assert mock_cleanup.call_count >= 2


class TestStartBackgroundTasks:
    """Tests for start_background_tasks."""

    def test_start_tasks_without_sandbox_manager(self):
        """Test starting background tasks without sandbox manager."""
        session_factory = Mock()

        with patch("nexus.server.background_tasks.asyncio.create_task") as mock_create:
            mock_create.return_value = Mock()
            tasks = start_background_tasks(session_factory, sandbox_manager=None)

            # Should create 1 task (session cleanup)
            assert len(tasks) == 1
            assert mock_create.call_count == 1

    def test_start_tasks_with_sandbox_manager(self):
        """Test starting background tasks with sandbox manager."""
        session_factory = Mock()
        sandbox_manager = Mock()

        with patch("nexus.server.background_tasks.asyncio.create_task") as mock_create:
            mock_create.return_value = Mock()
            tasks = start_background_tasks(session_factory, sandbox_manager=sandbox_manager)

            # Should create 2 tasks (session cleanup + sandbox cleanup)
            assert len(tasks) == 2
            assert mock_create.call_count == 2

    def test_start_tasks_returns_task_list(self):
        """Test that start_background_tasks returns a list of tasks."""
        session_factory = Mock()

        with patch("nexus.server.background_tasks.asyncio.create_task") as mock_create:
            mock_task1 = Mock()
            mock_task2 = Mock()
            mock_create.side_effect = [mock_task1, mock_task2]

            tasks = start_background_tasks(session_factory, sandbox_manager=Mock())

            assert mock_task1 in tasks
            assert mock_task2 in tasks
