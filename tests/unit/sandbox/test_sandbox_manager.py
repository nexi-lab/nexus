"""Unit tests for SandboxManager (Issue #1307 — Phase 1 safety net).

Tests cover:
- Sandbox creation: provider selection, DB persistence, name uniqueness
- Lifecycle operations: pause, resume, stop
- Listing and filtering
- Cleanup of expired sandboxes
- Error handling: DB failures, provider failures
- Metadata conversion
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.sandbox.sandbox_manager import SandboxManager
from nexus.sandbox.sandbox_provider import (
    CodeExecutionResult,
    SandboxInfo,
    SandboxNotFoundError,
)
from nexus.storage.models import Base, SandboxMetadataModel

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """Create in-memory SQLite database for testing."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session_factory(engine):
    """Create a session factory for SandboxManager."""
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def db_session(session_factory):
    """Create a database session."""
    session = session_factory()
    yield session
    session.close()


@pytest.fixture
def mock_provider():
    """Create a mock sandbox provider."""
    provider = AsyncMock()
    provider.create = AsyncMock(return_value="sandbox-123")
    provider.run_code = AsyncMock(
        return_value=CodeExecutionResult(stdout="hello", stderr="", exit_code=0, execution_time=0.5)
    )
    provider.pause = AsyncMock()
    provider.resume = AsyncMock()
    provider.destroy = AsyncMock()
    provider.get_info = AsyncMock(
        return_value=SandboxInfo(
            sandbox_id="sandbox-123",
            status="active",
            created_at=datetime.now(UTC),
            provider="docker",
        )
    )
    provider.mount_nexus = AsyncMock(
        return_value={"success": True, "mount_path": "/mnt/nexus", "message": "ok"}
    )
    return provider


@pytest.fixture
def manager(session_factory, mock_provider):
    """Create a SandboxManager with mocked providers."""
    mgr = SandboxManager.__new__(SandboxManager)
    mgr._session_factory = session_factory
    mgr.providers = {"docker": mock_provider}
    mgr._router = None
    return mgr


# ---------------------------------------------------------------------------
# Creation Tests
# ---------------------------------------------------------------------------


class TestCreateSandbox:
    @pytest.mark.asyncio
    async def test_create_sandbox_basic(self, manager, mock_provider):
        result = await manager.create_sandbox(
            name="test-sb",
            user_id="user-1",
            zone_id="zone-1",
            provider="docker",
        )

        assert result["name"] == "test-sb"
        assert result["user_id"] == "user-1"
        assert result["zone_id"] == "zone-1"
        assert result["status"] == "active"
        assert result["sandbox_id"] == "sandbox-123"
        mock_provider.create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_create_sandbox_with_agent_id(self, manager):
        result = await manager.create_sandbox(
            name="test-sb",
            user_id="user-1",
            zone_id="zone-1",
            agent_id="agent-42",
            provider="docker",
        )

        assert result["agent_id"] == "agent-42"

    @pytest.mark.asyncio
    async def test_create_sandbox_auto_selects_provider(self, manager):
        result = await manager.create_sandbox(
            name="test-sb",
            user_id="user-1",
            zone_id="zone-1",
        )

        assert result["provider"] == "docker"

    @pytest.mark.asyncio
    async def test_create_sandbox_no_providers_raises(self, session_factory):
        mgr = SandboxManager.__new__(SandboxManager)
        mgr._session_factory = session_factory
        mgr.providers = {}
        mgr._router = None

        with pytest.raises(ValueError, match="No sandbox providers available"):
            await mgr.create_sandbox(
                name="test-sb",
                user_id="user-1",
                zone_id="zone-1",
            )

    @pytest.mark.asyncio
    async def test_create_sandbox_invalid_provider_raises(self, manager):
        with pytest.raises(ValueError, match="Provider 'e2b' not available"):
            await manager.create_sandbox(
                name="test-sb",
                user_id="user-1",
                zone_id="zone-1",
                provider="e2b",
            )

    @pytest.mark.asyncio
    async def test_create_sandbox_duplicate_name_raises(self, manager):
        await manager.create_sandbox(
            name="test-sb",
            user_id="user-1",
            zone_id="zone-1",
            provider="docker",
        )

        # Second creation with same name should fail
        manager.providers["docker"].create = AsyncMock(return_value="sandbox-456")
        with pytest.raises(ValueError, match="already exists"):
            await manager.create_sandbox(
                name="test-sb",
                user_id="user-1",
                zone_id="zone-1",
                provider="docker",
            )

    @pytest.mark.asyncio
    async def test_create_sandbox_ttl_sets_expiry(self, manager):
        result = await manager.create_sandbox(
            name="test-sb",
            user_id="user-1",
            zone_id="zone-1",
            ttl_minutes=30,
            provider="docker",
        )

        assert result["ttl_minutes"] == 30
        assert result["expires_at"] is not None

    @pytest.mark.asyncio
    async def test_create_sandbox_prewarm_failure_non_fatal(self, manager, mock_provider):
        mock_provider.prewarm_imports = AsyncMock(side_effect=RuntimeError("prewarm failed"))

        # Should still succeed despite prewarm failure
        result = await manager.create_sandbox(
            name="test-sb",
            user_id="user-1",
            zone_id="zone-1",
            provider="docker",
        )

        assert result["status"] == "active"


# ---------------------------------------------------------------------------
# Lifecycle Tests
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def _create_sandbox(self, manager):
        return await manager.create_sandbox(
            name="lifecycle-sb",
            user_id="user-1",
            zone_id="zone-1",
            provider="docker",
        )

    @pytest.mark.asyncio
    async def test_pause_sandbox(self, manager):
        created = await self._create_sandbox(manager)
        result = await manager.pause_sandbox(created["sandbox_id"])

        assert result["status"] == "paused"
        assert result["paused_at"] is not None
        assert result["expires_at"] is None  # No expiry while paused

    @pytest.mark.asyncio
    async def test_resume_sandbox(self, manager):
        created = await self._create_sandbox(manager)
        await manager.pause_sandbox(created["sandbox_id"])
        result = await manager.resume_sandbox(created["sandbox_id"])

        assert result["status"] == "active"
        assert result["expires_at"] is not None

    @pytest.mark.asyncio
    async def test_stop_sandbox(self, manager, mock_provider):
        created = await self._create_sandbox(manager)
        result = await manager.stop_sandbox(created["sandbox_id"])

        assert result["status"] == "stopped"
        assert result["stopped_at"] is not None
        assert result["expires_at"] is None
        mock_provider.destroy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_nonexistent_raises(self, manager):
        with pytest.raises(SandboxNotFoundError):
            await manager.stop_sandbox("nonexistent-id")

    @pytest.mark.asyncio
    async def test_get_sandbox_status(self, manager):
        created = await self._create_sandbox(manager)
        result = await manager.get_sandbox_status(created["sandbox_id"])

        assert result["sandbox_id"] == created["sandbox_id"]
        assert result["status"] == "active"


# ---------------------------------------------------------------------------
# Run Code Tests
# ---------------------------------------------------------------------------


class TestRunCode:
    @pytest.mark.asyncio
    async def test_run_code_basic(self, manager, mock_provider):
        created = await manager.create_sandbox(
            name="run-sb",
            user_id="user-1",
            zone_id="zone-1",
            provider="docker",
        )

        result = await manager.run_code(
            sandbox_id=created["sandbox_id"],
            language="python",
            code="print('hello')",
        )

        assert result.stdout == "hello"
        assert result.exit_code == 0
        mock_provider.run_code.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_code_nonexistent_raises(self, manager):
        with pytest.raises(SandboxNotFoundError):
            await manager.run_code(
                sandbox_id="nonexistent",
                language="python",
                code="print('hello')",
            )


# ---------------------------------------------------------------------------
# Listing Tests
# ---------------------------------------------------------------------------


class TestListSandboxes:
    @pytest.mark.asyncio
    async def test_list_all(self, manager):
        manager.providers["docker"].create = AsyncMock(side_effect=["sandbox-1", "sandbox-2"])
        await manager.create_sandbox(
            name="sb-1", user_id="user-1", zone_id="zone-1", provider="docker"
        )
        await manager.create_sandbox(
            name="sb-2", user_id="user-1", zone_id="zone-1", provider="docker"
        )

        result = await manager.list_sandboxes()
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_list_filter_by_user(self, manager):
        manager.providers["docker"].create = AsyncMock(side_effect=["sandbox-1", "sandbox-2"])
        await manager.create_sandbox(
            name="sb-1", user_id="user-1", zone_id="zone-1", provider="docker"
        )
        await manager.create_sandbox(
            name="sb-2", user_id="user-2", zone_id="zone-1", provider="docker"
        )

        result = await manager.list_sandboxes(user_id="user-1")
        assert len(result) == 1
        assert result[0]["user_id"] == "user-1"

    @pytest.mark.asyncio
    async def test_list_filter_by_status(self, manager):
        created = await manager.create_sandbox(
            name="sb-1", user_id="user-1", zone_id="zone-1", provider="docker"
        )
        await manager.stop_sandbox(created["sandbox_id"])

        active = await manager.list_sandboxes(status="active")
        stopped = await manager.list_sandboxes(status="stopped")

        assert len(active) == 0
        assert len(stopped) == 1

    @pytest.mark.asyncio
    async def test_list_filter_by_agent_id(self, manager):
        manager.providers["docker"].create = AsyncMock(side_effect=["sandbox-1", "sandbox-2"])
        await manager.create_sandbox(
            name="sb-1",
            user_id="user-1",
            zone_id="zone-1",
            agent_id="agent-1",
            provider="docker",
        )
        await manager.create_sandbox(
            name="sb-2",
            user_id="user-1",
            zone_id="zone-1",
            agent_id="agent-2",
            provider="docker",
        )

        result = await manager.list_sandboxes(agent_id="agent-1")
        assert len(result) == 1
        assert result[0]["agent_id"] == "agent-1"


# ---------------------------------------------------------------------------
# Cleanup Tests
# ---------------------------------------------------------------------------


class TestCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_expired_sandboxes(self, manager, db_session):
        created = await manager.create_sandbox(
            name="expired-sb",
            user_id="user-1",
            zone_id="zone-1",
            ttl_minutes=1,
            provider="docker",
        )

        # Manually expire the sandbox by setting expires_at in the past
        metadata = (
            db_session.query(SandboxMetadataModel).filter_by(sandbox_id=created["sandbox_id"]).one()
        )
        metadata.expires_at = datetime.now(UTC) - timedelta(minutes=5)
        db_session.commit()

        count = await manager.cleanup_expired_sandboxes()
        assert count == 1

    @pytest.mark.asyncio
    async def test_cleanup_skips_active_unexpired(self, manager):
        await manager.create_sandbox(
            name="active-sb",
            user_id="user-1",
            zone_id="zone-1",
            ttl_minutes=60,
            provider="docker",
        )

        count = await manager.cleanup_expired_sandboxes()
        assert count == 0


# ---------------------------------------------------------------------------
# Get Or Create Tests
# ---------------------------------------------------------------------------


class TestGetOrCreate:
    @pytest.mark.asyncio
    async def test_get_existing_sandbox(self, manager, mock_provider):
        created = await manager.create_sandbox(
            name="reuse-sb",
            user_id="user-1",
            zone_id="zone-1",
            provider="docker",
        )

        result = await manager.get_or_create_sandbox(
            name="reuse-sb",
            user_id="user-1",
            zone_id="zone-1",
            provider="docker",
            verify_status=True,
        )

        assert result["sandbox_id"] == created["sandbox_id"]
        # Provider should not have been called for a second create
        assert mock_provider.create.await_count == 1

    @pytest.mark.asyncio
    async def test_create_when_none_exists(self, manager, mock_provider):
        mock_provider.create = AsyncMock(return_value="new-sandbox")
        result = await manager.get_or_create_sandbox(
            name="new-sb",
            user_id="user-1",
            zone_id="zone-1",
            provider="docker",
        )

        assert result["sandbox_id"] == "new-sandbox"


# ---------------------------------------------------------------------------
# Connect/Disconnect Tests
# ---------------------------------------------------------------------------


class TestConnect:
    @pytest.mark.asyncio
    async def test_connect_sandbox(self, manager, mock_provider):
        created = await manager.create_sandbox(
            name="connect-sb",
            user_id="user-1",
            zone_id="zone-1",
            provider="docker",
        )

        result = await manager.connect_sandbox(
            sandbox_id=created["sandbox_id"],
            provider="docker",
            nexus_url="http://localhost:2026",
            nexus_api_key="test-key",
        )

        assert result["success"] is True
        assert result["mount_path"] == "/mnt/nexus"

    @pytest.mark.asyncio
    async def test_connect_requires_nexus_url(self, manager):
        with pytest.raises(ValueError, match="nexus_url and nexus_api_key required"):
            await manager.connect_sandbox(
                sandbox_id="sandbox-123",
                provider="docker",
            )

    @pytest.mark.asyncio
    async def test_connect_invalid_provider_raises(self, manager):
        with pytest.raises(ValueError, match="not available"):
            await manager.connect_sandbox(
                sandbox_id="sandbox-123",
                provider="e2b",
                nexus_url="http://localhost:2026",
                nexus_api_key="test-key",
            )


# ---------------------------------------------------------------------------
# Metadata Conversion Tests
# ---------------------------------------------------------------------------


class TestMetadataConversion:
    @pytest.mark.asyncio
    async def test_metadata_to_dict_contains_all_fields(self, manager):
        created = await manager.create_sandbox(
            name="meta-sb",
            user_id="user-1",
            zone_id="zone-1",
            agent_id="agent-1",
            provider="docker",
        )

        expected_keys = {
            "sandbox_id",
            "name",
            "user_id",
            "agent_id",
            "zone_id",
            "provider",
            "template_id",
            "status",
            "created_at",
            "last_active_at",
            "paused_at",
            "stopped_at",
            "ttl_minutes",
            "expires_at",
            "uptime_seconds",
        }
        assert set(created.keys()) == expected_keys

    @pytest.mark.asyncio
    async def test_uptime_seconds_is_positive(self, manager):
        created = await manager.create_sandbox(
            name="uptime-sb",
            user_id="user-1",
            zone_id="zone-1",
            provider="docker",
        )

        assert created["uptime_seconds"] >= 0
