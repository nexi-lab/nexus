"""Execution tests for MoveAction and MetadataAction (Issue #3063).

Verifies that async service methods are properly awaited and that
side effects actually occur. These tests complement the existing
config/interpolation tests in test_actions.py.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.workflows.actions import MetadataAction, MoveAction
from nexus.bricks.workflows.types import TriggerType, WorkflowContext


def _make_context(
    *,
    file_path: str = "/docs/readme.md",
    services: MagicMock | None = None,
) -> WorkflowContext:
    """Create a WorkflowContext with optional mock services."""
    return WorkflowContext(
        workflow_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        zone_id="test-zone",
        trigger_type=TriggerType.FILE_WRITE,
        file_path=file_path,
        services=services,
    )


def _make_nexus_ops_services() -> MagicMock:
    """Create mock services with AsyncMock nexus_ops."""
    services = MagicMock()
    services.nexus_ops = MagicMock()
    services.nexus_ops.rename = AsyncMock()
    services.nexus_ops.mkdir = AsyncMock()
    return services


def _make_metadata_services(*, path_id: int = 42) -> MagicMock:
    """Create mock services with AsyncMock metadata_store."""
    services = MagicMock()
    mock_path_rec = MagicMock()
    mock_path_rec.path_id = path_id
    services.metadata_store = MagicMock()
    services.metadata_store.get_path = AsyncMock(return_value=mock_path_rec)
    services.metadata_store.set_file_metadata = AsyncMock()
    return services


# ============================================================================
# MoveAction execution tests
# ============================================================================


class TestMoveActionExecution:
    """Test that MoveAction properly awaits service calls."""

    @pytest.mark.asyncio
    async def test_rename_is_awaited(self) -> None:
        """Regression test: rename must be awaited (Issue #3063 §2)."""
        services = _make_nexus_ops_services()
        context = _make_context(services=services)
        action = MoveAction(
            name="mv",
            config={"source": "/old.txt", "destination": "/new.txt"},
        )

        result = await action.execute(context)

        assert result.success is True
        services.nexus_ops.rename.assert_awaited_once_with("/old.txt", "/new.txt")

    @pytest.mark.asyncio
    async def test_create_parents_calls_mkdir(self) -> None:
        """When create_parents=True, mkdir should be awaited via VFS."""
        services = _make_nexus_ops_services()
        context = _make_context(services=services)
        action = MoveAction(
            name="mv",
            config={
                "source": "/old.txt",
                "destination": "/archive/2024/file.txt",
                "create_parents": True,
            },
        )

        result = await action.execute(context)

        assert result.success is True
        # mkdir called with parent directory
        services.nexus_ops.mkdir.assert_awaited_once_with("/archive/2024", parents=True)
        services.nexus_ops.rename.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_create_parents_false_skips_mkdir(self) -> None:
        services = _make_nexus_ops_services()
        context = _make_context(services=services)
        action = MoveAction(
            name="mv",
            config={"source": "/old.txt", "destination": "/new.txt"},
        )

        await action.execute(context)

        services.nexus_ops.mkdir.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mkdir_failure_does_not_block_rename(self) -> None:
        """mkdir errors are swallowed — rename will surface real errors."""
        services = _make_nexus_ops_services()
        services.nexus_ops.mkdir.side_effect = Exception("already exists")
        context = _make_context(services=services)
        action = MoveAction(
            name="mv",
            config={
                "source": "/old.txt",
                "destination": "/archive/file.txt",
                "create_parents": True,
            },
        )

        result = await action.execute(context)

        assert result.success is True
        services.nexus_ops.rename.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rename_failure_returns_error(self) -> None:
        services = _make_nexus_ops_services()
        services.nexus_ops.rename.side_effect = Exception("permission denied")
        context = _make_context(services=services)
        action = MoveAction(
            name="mv",
            config={"source": "/old.txt", "destination": "/new.txt"},
        )

        result = await action.execute(context)

        assert result.success is False
        assert "Move action failed" in result.error

    @pytest.mark.asyncio
    async def test_interpolation_with_services(self) -> None:
        """Variable interpolation works when services are available."""
        services = _make_nexus_ops_services()
        context = _make_context(file_path="/docs/readme.md", services=services)
        action = MoveAction(
            name="mv",
            config={
                "source": "{file_path}",
                "destination": "/archive/{filename}",
            },
        )

        result = await action.execute(context)

        assert result.success is True
        services.nexus_ops.rename.assert_awaited_once_with("/docs/readme.md", "/archive/readme.md")

    @pytest.mark.asyncio
    async def test_output_contains_source_and_destination(self) -> None:
        services = _make_nexus_ops_services()
        context = _make_context(services=services)
        action = MoveAction(
            name="mv",
            config={"source": "/a.txt", "destination": "/b.txt"},
        )

        result = await action.execute(context)

        assert result.output["source"] == "/a.txt"
        assert result.output["destination"] == "/b.txt"


# ============================================================================
# MetadataAction execution tests
# ============================================================================


class TestMetadataActionExecution:
    """Test that MetadataAction properly awaits service calls."""

    @pytest.mark.asyncio
    async def test_set_metadata_is_awaited(self) -> None:
        """Regression test: set_file_metadata must be awaited (Issue #3063 §2)."""
        services = _make_metadata_services(path_id=42)
        context = _make_context(services=services)
        action = MetadataAction(
            name="meta",
            config={"metadata": {"status": "done"}},
        )

        result = await action.execute(context)

        assert result.success is True
        services.metadata_store.set_file_metadata.assert_awaited_once_with(42, "status", "done")

    @pytest.mark.asyncio
    async def test_get_path_is_awaited(self) -> None:
        """Regression test: get_path must be awaited (Issue #3063 §2)."""
        services = _make_metadata_services()
        context = _make_context(services=services)
        action = MetadataAction(
            name="meta",
            config={"metadata": {"key": "val"}},
        )

        await action.execute(context)

        services.metadata_store.get_path.assert_awaited_once_with("/docs/readme.md")

    @pytest.mark.asyncio
    async def test_get_path_called_once_not_per_key(self) -> None:
        """N+1 regression test: get_path hoisted outside loop (Issue #3063 §13)."""
        services = _make_metadata_services()
        context = _make_context(services=services)
        action = MetadataAction(
            name="meta",
            config={"metadata": {"a": "1", "b": "2", "c": "3"}},
        )

        await action.execute(context)

        # get_path should be called exactly once, not 3 times
        assert services.metadata_store.get_path.await_count == 1
        assert services.metadata_store.set_file_metadata.await_count == 3

    @pytest.mark.asyncio
    async def test_missing_path_record_skips_metadata_set(self) -> None:
        services = _make_metadata_services()
        services.metadata_store.get_path = AsyncMock(return_value=None)
        context = _make_context(services=services)
        action = MetadataAction(
            name="meta",
            config={"metadata": {"key": "val"}},
        )

        result = await action.execute(context)

        assert result.success is True
        services.metadata_store.set_file_metadata.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_metadata_interpolation(self) -> None:
        """Variable interpolation in metadata values."""
        services = _make_metadata_services(path_id=99)
        context = _make_context(file_path="/docs/readme.md", services=services)
        action = MetadataAction(
            name="meta",
            config={"metadata": {"source": "{filename}"}},
        )

        await action.execute(context)

        services.metadata_store.set_file_metadata.assert_awaited_once_with(
            99, "source", "readme.md"
        )

    @pytest.mark.asyncio
    async def test_set_metadata_failure_returns_error(self) -> None:
        services = _make_metadata_services()
        services.metadata_store.set_file_metadata.side_effect = Exception("db error")
        context = _make_context(services=services)
        action = MetadataAction(
            name="meta",
            config={"metadata": {"key": "val"}},
        )

        result = await action.execute(context)

        assert result.success is False
        assert "Metadata action failed" in result.error

    @pytest.mark.asyncio
    async def test_output_contains_metadata(self) -> None:
        services = _make_metadata_services()
        context = _make_context(services=services)
        action = MetadataAction(
            name="meta",
            config={"metadata": {"status": "processed", "version": "2"}},
        )

        result = await action.execute(context)

        assert result.output["metadata"] == {"status": "processed", "version": "2"}
