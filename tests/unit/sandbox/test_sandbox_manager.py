"""Unit tests for SandboxManager (Issue #2051).

Tests the thin orchestrator: escalation handling, race condition paths,
parallel verify in list_sandboxes, and disconnect_sandbox.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.bricks.sandbox.provider_registry import ProviderRegistry
from nexus.bricks.sandbox.repository import SandboxRepository
from nexus.bricks.sandbox.sandbox_manager import SandboxManager
from nexus.bricks.sandbox.sandbox_provider import (
    CodeExecutionResult,
    EscalationNeeded,
    SandboxInfo,
    SandboxNotFoundError,
    SandboxProvider,
)

# -- Fixtures ---------------------------------------------------------------


class FakeProvider(SandboxProvider):
    """Minimal async SandboxProvider for testing."""

    def __init__(self, name: str = "fake") -> None:
        self.name = name

    async def create(
        self, template_id=None, timeout_minutes=10, metadata=None, security_profile=None
    ) -> str:
        return f"sandbox-{self.name}-001"

    async def run_code(self, sandbox_id, language, code, timeout=300, as_script=False):
        return CodeExecutionResult(stdout="ok", stderr="", exit_code=0, execution_time=0.1)

    async def pause(self, sandbox_id: str) -> None:
        pass

    async def resume(self, sandbox_id: str) -> None:
        pass

    async def destroy(self, sandbox_id: str) -> None:
        pass

    async def get_info(self, sandbox_id: str):
        return SandboxInfo(
            sandbox_id=sandbox_id,
            status="active",
            created_at=datetime.now(UTC),
            provider=self.name,
        )

    async def is_available(self) -> bool:
        return True

    async def mount_nexus(
        self,
        sandbox_id,
        mount_path,
        nexus_url,
        api_key,
        agent_id=None,
        skip_dependency_checks=False,
    ):
        return {"success": True, "mount_path": mount_path, "message": "ok", "files_visible": 5}


class EscalatingProvider(FakeProvider):
    """Provider that raises EscalationNeeded on run_code."""

    def __init__(self, reason: str = "needs docker", suggested_tier: str | None = "docker") -> None:
        super().__init__("monty")
        self._reason = reason
        self._suggested_tier = suggested_tier

    async def run_code(self, sandbox_id, language, code, timeout=300, as_script=False):
        raise EscalationNeeded(reason=self._reason, suggested_tier=self._suggested_tier)


def _make_metadata(
    sandbox_id: str = "sb-001",
    name: str = "test-sandbox",
    user_id: str = "user-1",
    provider: str = "docker",
    status: str = "active",
    ttl_minutes: int = 10,
) -> dict[str, Any]:
    """Create a fake metadata dict."""
    now = datetime.now(UTC)
    return {
        "sandbox_id": sandbox_id,
        "name": name,
        "user_id": user_id,
        "agent_id": None,
        "zone_id": "zone-1",
        "provider": provider,
        "template_id": None,
        "status": status,
        "created_at": now.isoformat(),
        "last_active_at": now.isoformat(),
        "paused_at": None,
        "stopped_at": None,
        "ttl_minutes": ttl_minutes,
        "expires_at": (now + timedelta(minutes=ttl_minutes)).isoformat(),
        "uptime_seconds": 0.0,
    }


def _make_manager(
    providers: dict[str, SandboxProvider] | None = None,
    repo_overrides: dict[str, Any] | None = None,
) -> SandboxManager:
    """Build a SandboxManager with mocked repository and registry."""
    # Build registry
    registry = ProviderRegistry()
    if providers:
        for name, prov in providers.items():
            registry.register(name, prov)

    # Build mock repository
    repo = MagicMock(spec=SandboxRepository)
    repo.get_metadata.return_value = _make_metadata()
    repo.find_active_by_name.return_value = None
    repo.create_metadata.return_value = _make_metadata()
    repo.update_metadata.return_value = _make_metadata()
    repo.list_sandboxes.return_value = []
    repo.find_expired.return_value = []

    # Apply overrides
    if repo_overrides:
        for attr, value in repo_overrides.items():
            setattr(repo, attr, value)

    record_store = SimpleNamespace(session_factory=MagicMock())
    return SandboxManager(
        record_store=record_store,
        repository=repo,
        registry=registry,
    )


# -- Escalation Tests -------------------------------------------------------


class TestEscalationHandling:
    """Tests for _handle_escalation (Issue #2051 #10A)."""

    @pytest.mark.asyncio
    async def test_escalation_to_suggested_tier(self):
        """Escalation goes to suggested_tier when available."""
        monty = EscalatingProvider(reason="needs docker", suggested_tier="docker")
        docker = FakeProvider("docker")

        mgr = _make_manager(providers={"monty": monty, "docker": docker})
        mgr._repository.get_metadata.return_value = _make_metadata(provider="monty")
        mgr.wire_router()

        result = await mgr.run_code("sb-001", "python", "print(1)")

        assert result.exit_code == 0
        assert result.stdout == "ok"

    @pytest.mark.asyncio
    async def test_escalation_creates_and_destroys_temp_sandbox(self):
        """Escalation creates a temp sandbox and destroys it after."""
        monty = EscalatingProvider(reason="needs docker", suggested_tier="docker")
        docker = FakeProvider("docker")
        docker.create = AsyncMock(return_value="temp-sb-123")
        docker.destroy = AsyncMock()

        mgr = _make_manager(providers={"monty": monty, "docker": docker})
        mgr._repository.get_metadata.return_value = _make_metadata(provider="monty")
        mgr.wire_router()

        await mgr.run_code("sb-001", "python", "print(1)")

        docker.create.assert_called_once()
        docker.destroy.assert_called_once_with("temp-sb-123")

    @pytest.mark.asyncio
    async def test_escalation_temp_sandbox_cleaned_up_on_failure(self):
        """Temp sandbox is destroyed even if run_code fails on next tier."""
        monty = EscalatingProvider(reason="needs docker", suggested_tier="docker")
        docker = FakeProvider("docker")
        docker.create = AsyncMock(return_value="temp-sb-123")
        docker.run_code = AsyncMock(side_effect=RuntimeError("execution failed"))
        docker.destroy = AsyncMock()

        mgr = _make_manager(providers={"monty": monty, "docker": docker})
        mgr._repository.get_metadata.return_value = _make_metadata(provider="monty")
        mgr.wire_router()

        with pytest.raises(RuntimeError, match="execution failed"):
            await mgr.run_code("sb-001", "python", "print(1)")

        docker.destroy.assert_called_once_with("temp-sb-123")

    @pytest.mark.asyncio
    async def test_escalation_raises_when_no_router(self):
        """EscalationNeeded propagates when no router is wired."""
        monty = EscalatingProvider(reason="needs docker", suggested_tier="docker")

        mgr = _make_manager(providers={"monty": monty})
        mgr._repository.get_metadata.return_value = _make_metadata(provider="monty")
        # No wire_router() call

        with pytest.raises(EscalationNeeded, match="needs docker"):
            await mgr.run_code("sb-001", "python", "print(1)")

    @pytest.mark.asyncio
    async def test_escalation_raises_when_no_next_tier(self):
        """EscalationNeeded propagates when suggested tier is not available."""
        monty = EscalatingProvider(reason="needs docker", suggested_tier="e2b")

        mgr = _make_manager(providers={"monty": monty})
        mgr._repository.get_metadata.return_value = _make_metadata(provider="monty")
        mgr.wire_router()

        with pytest.raises(EscalationNeeded, match="needs docker"):
            await mgr.run_code("sb-001", "python", "print(1)")

    @pytest.mark.asyncio
    async def test_escalation_rewires_host_functions_from_monty(self):
        """Host functions are re-wired when escalating from monty."""
        monty = EscalatingProvider(reason="needs docker", suggested_tier="docker")
        docker = FakeProvider("docker")
        docker.create = AsyncMock(return_value="temp-sb-123")
        docker.destroy = AsyncMock()
        docker.set_host_functions = MagicMock()

        mgr = _make_manager(providers={"monty": monty, "docker": docker})
        mgr.wire_router()

        # Cache host functions for the agent — metadata must say "monty"
        # so run_code fetches the monty provider and triggers escalation
        mgr._repository.get_metadata.return_value = {
            **_make_metadata(provider="monty", sandbox_id="sb-001"),
            "agent_id": "agent-1",
        }
        host_fns = {"read_file": lambda path: path}
        mgr._router.cache_host_functions("agent-1", host_fns)

        await mgr.run_code("sb-001", "python", "print(1)")

        docker.set_host_functions.assert_called_once_with("temp-sb-123", host_fns)


# -- Race Condition Tests ----------------------------------------------------


class TestRaceCondition:
    """Tests for get_or_create_sandbox race condition handling (Issue #2051 #11A)."""

    @pytest.mark.asyncio
    async def test_returns_existing_verified_sandbox(self):
        """Returns existing sandbox when verified as active."""
        docker = FakeProvider("docker")

        existing_meta = _make_metadata(sandbox_id="sb-existing")
        repo_overrides = {
            "find_active_by_name": MagicMock(return_value=existing_meta),
        }
        mgr = _make_manager(providers={"docker": docker}, repo_overrides=repo_overrides)

        result = await mgr.get_or_create_sandbox(name="test", user_id="user-1", zone_id="zone-1")

        assert result["sandbox_id"] == "sb-existing"
        assert result["verified"] is True

    @pytest.mark.asyncio
    async def test_creates_new_when_none_found(self):
        """Creates new sandbox when no existing one found."""
        docker = FakeProvider("docker")

        mgr = _make_manager(providers={"docker": docker})

        with patch(
            "nexus.bricks.sandbox.sandbox_manager.SandboxManager.create_sandbox",
            new_callable=AsyncMock,
        ) as mock_create:
            mock_create.return_value = _make_metadata(sandbox_id="sb-new")

            result = await mgr.get_or_create_sandbox(
                name="test", user_id="user-1", zone_id="zone-1"
            )

        assert result["sandbox_id"] == "sb-new"

    @pytest.mark.asyncio
    async def test_handles_stale_sandbox_mismatch(self):
        """Marks sandbox as stopped when provider says it's not active."""
        docker = FakeProvider("docker")
        docker.get_info = AsyncMock(
            return_value=SandboxInfo(
                sandbox_id="sb-stale",
                status="stopped",
                created_at=datetime.now(UTC),
                provider="docker",
            )
        )

        existing_meta = _make_metadata(sandbox_id="sb-stale")
        repo_overrides = {
            "find_active_by_name": MagicMock(return_value=existing_meta),
        }
        mgr = _make_manager(providers={"docker": docker}, repo_overrides=repo_overrides)

        with patch(
            "nexus.bricks.sandbox.sandbox_manager.SandboxManager.create_sandbox",
            new_callable=AsyncMock,
        ) as mock_create:
            mock_create.return_value = _make_metadata(sandbox_id="sb-new")

            result = await mgr.get_or_create_sandbox(
                name="test", user_id="user-1", zone_id="zone-1"
            )

        # Should have marked stale as stopped
        mgr._repository.update_metadata.assert_called()
        # Should have created new
        assert result["sandbox_id"] == "sb-new"

    @pytest.mark.asyncio
    async def test_race_condition_retries_with_new_name(self):
        """On 'already exists' error, marks stale and retries with timestamped name."""
        docker = FakeProvider("docker")

        mgr = _make_manager(providers={"docker": docker})
        # First find returns None, then find after race returns the stale sandbox
        mgr._repository.find_active_by_name.side_effect = [
            None,  # First check: no existing sandbox
            _make_metadata(sandbox_id="sb-stale"),  # Stale cleanup lookup
        ]

        call_count = {"n": 0}

        async def _mock_create(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ValueError("Active sandbox with name 'test' already exists for user user-1")
            return _make_metadata(sandbox_id="sb-retry")

        with patch.object(mgr, "create_sandbox", side_effect=_mock_create):
            result = await mgr.get_or_create_sandbox(
                name="test", user_id="user-1", zone_id="zone-1"
            )

        assert result["sandbox_id"] == "sb-retry"

    @pytest.mark.asyncio
    async def test_returns_existing_without_verify(self):
        """Returns existing sandbox without verification when verify_status=False."""
        docker = FakeProvider("docker")

        existing_meta = _make_metadata(sandbox_id="sb-existing")
        repo_overrides = {
            "find_active_by_name": MagicMock(return_value=existing_meta),
        }
        mgr = _make_manager(providers={"docker": docker}, repo_overrides=repo_overrides)

        result = await mgr.get_or_create_sandbox(
            name="test", user_id="user-1", zone_id="zone-1", verify_status=False
        )

        assert result["sandbox_id"] == "sb-existing"
        assert "verified" not in result


# -- Parallel Verify Tests ---------------------------------------------------


class TestParallelVerify:
    """Tests for asyncio.gather-based parallel verify in list_sandboxes."""

    @pytest.mark.asyncio
    async def test_list_sandboxes_without_verify(self):
        """list_sandboxes returns DB results without verification."""
        mgr = _make_manager(providers={"docker": FakeProvider("docker")})
        sandboxes = [_make_metadata(sandbox_id=f"sb-{i}") for i in range(3)]
        mgr._repository.list_sandboxes.return_value = sandboxes

        result = await mgr.list_sandboxes(user_id="user-1")

        assert len(result) == 3
        assert all("verified" not in sb for sb in result)

    @pytest.mark.asyncio
    async def test_list_sandboxes_with_verify_marks_verified(self):
        """list_sandboxes with verify_status=True marks each sandbox."""
        docker = FakeProvider("docker")

        mgr = _make_manager(providers={"docker": docker})
        sandboxes = [_make_metadata(sandbox_id=f"sb-{i}") for i in range(3)]
        mgr._repository.list_sandboxes.return_value = sandboxes

        result = await mgr.list_sandboxes(user_id="user-1", verify_status=True)

        assert len(result) == 3
        assert all(sb.get("verified") is True for sb in result)
        assert all(sb.get("provider_status") == "active" for sb in result)

    @pytest.mark.asyncio
    async def test_list_sandboxes_verify_handles_missing_provider(self):
        """Sandboxes with unavailable provider get verified=False."""
        mgr = _make_manager(providers={"docker": FakeProvider("docker")})
        sandboxes = [_make_metadata(sandbox_id="sb-1", provider="e2b")]
        mgr._repository.list_sandboxes.return_value = sandboxes

        result = await mgr.list_sandboxes(verify_status=True)

        assert result[0]["verified"] is False

    @pytest.mark.asyncio
    async def test_list_sandboxes_verify_updates_stale_status(self):
        """Status mismatch triggers DB update via parallel verify."""
        docker = FakeProvider("docker")
        docker.get_info = AsyncMock(
            return_value=SandboxInfo(
                sandbox_id="sb-1",
                status="stopped",
                created_at=datetime.now(UTC),
                provider="docker",
            )
        )

        mgr = _make_manager(providers={"docker": docker})
        sandboxes = [_make_metadata(sandbox_id="sb-1", status="active")]
        mgr._repository.list_sandboxes.return_value = sandboxes

        result = await mgr.list_sandboxes(verify_status=True)

        assert result[0]["status"] == "stopped"
        mgr._repository.update_metadata.assert_called()

    @pytest.mark.asyncio
    async def test_list_sandboxes_verify_marks_not_found_as_stopped(self):
        """Sandbox not found in provider gets marked as stopped."""
        docker = FakeProvider("docker")
        docker.get_info = AsyncMock(side_effect=SandboxNotFoundError("not found"))

        mgr = _make_manager(providers={"docker": docker})
        sandboxes = [_make_metadata(sandbox_id="sb-1", status="active")]
        mgr._repository.list_sandboxes.return_value = sandboxes

        result = await mgr.list_sandboxes(verify_status=True)

        assert result[0]["status"] == "stopped"
        assert result[0]["verified"] is True


# -- Disconnect Tests --------------------------------------------------------


class TestDisconnectSandbox:
    """Tests for disconnect_sandbox with actual unmount (Issue #2051 #6A)."""

    @pytest.mark.asyncio
    async def test_disconnect_calls_unmount_if_available(self):
        """disconnect_sandbox calls unmount_nexus when provider supports it."""
        docker = FakeProvider("docker")
        docker.unmount_nexus = AsyncMock(
            return_value={"success": True, "mount_path": "/mnt/nexus", "message": "ok"}
        )

        mgr = _make_manager(providers={"docker": docker})

        result = await mgr.disconnect_sandbox(
            sandbox_id="sb-1", provider="docker", sandbox_api_key="key123"
        )

        assert result["success"] is True
        docker.unmount_nexus.assert_called_once_with("sb-1")

    @pytest.mark.asyncio
    async def test_disconnect_raises_without_api_key(self):
        """disconnect_sandbox requires an API key."""
        mgr = _make_manager(providers={"docker": FakeProvider("docker")})

        with pytest.raises(ValueError, match="API key required"):
            await mgr.disconnect_sandbox(sandbox_id="sb-1", provider="docker", sandbox_api_key=None)

    @pytest.mark.asyncio
    async def test_disconnect_succeeds_without_unmount_support(self):
        """disconnect_sandbox succeeds even if provider lacks unmount."""
        docker = FakeProvider("docker")
        # FakeProvider doesn't have unmount_nexus method beyond mount_nexus

        mgr = _make_manager(providers={"docker": docker})

        result = await mgr.disconnect_sandbox(
            sandbox_id="sb-1", provider="docker", sandbox_api_key="key123"
        )

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_disconnect_raises_for_unknown_provider(self):
        """disconnect_sandbox raises ValueError for unregistered provider."""
        mgr = _make_manager(providers={"docker": FakeProvider("docker")})

        with pytest.raises(ValueError, match="not available"):
            await mgr.disconnect_sandbox(
                sandbox_id="sb-1", provider="e2b", sandbox_api_key="key123"
            )


# -- Providers Property Tests ------------------------------------------------


class TestProvidersProperty:
    """Tests for backward-compatible providers dict property."""

    def test_providers_returns_initialized_providers(self):
        """providers property returns dict of initialized providers."""
        docker = FakeProvider("docker")
        mgr = _make_manager(providers={"docker": docker})

        result = mgr.providers

        assert "docker" in result
        assert result["docker"] is docker

    def test_providers_returns_empty_when_none_registered(self):
        """providers property returns empty dict when no providers."""
        mgr = _make_manager(providers={})

        result = mgr.providers

        assert result == {}


# -- Cleanup Tests -----------------------------------------------------------


class TestCleanupExpired:
    """Tests for cleanup_expired_sandboxes."""

    @pytest.mark.asyncio
    async def test_cleanup_stops_expired_sandboxes(self):
        """cleanup_expired_sandboxes calls stop_sandbox for each expired."""
        docker = FakeProvider("docker")
        mgr = _make_manager(providers={"docker": docker})
        mgr._repository.find_expired.return_value = ["sb-1", "sb-2"]

        with patch.object(mgr, "stop_sandbox", new_callable=AsyncMock) as mock_stop:
            count = await mgr.cleanup_expired_sandboxes()

        assert count == 2
        assert mock_stop.call_count == 2

    @pytest.mark.asyncio
    async def test_cleanup_returns_zero_when_none_expired(self):
        """cleanup_expired_sandboxes returns 0 when nothing expired."""
        mgr = _make_manager(providers={"docker": FakeProvider("docker")})
        mgr._repository.find_expired.return_value = []

        count = await mgr.cleanup_expired_sandboxes()

        assert count == 0

    @pytest.mark.asyncio
    async def test_cleanup_continues_on_individual_failure(self):
        """cleanup_expired_sandboxes skips failing sandboxes and continues."""
        docker = FakeProvider("docker")
        mgr = _make_manager(providers={"docker": docker})
        mgr._repository.find_expired.return_value = ["sb-1", "sb-2", "sb-3"]

        call_count = {"n": 0}

        async def _mock_stop(sb_id):
            call_count["n"] += 1
            if sb_id == "sb-2":
                raise RuntimeError("provider down")
            return _make_metadata(sandbox_id=sb_id, status="stopped")

        with patch.object(mgr, "stop_sandbox", side_effect=_mock_stop):
            count = await mgr.cleanup_expired_sandboxes()

        assert count == 2  # sb-1 and sb-3 succeeded
        assert call_count["n"] == 3  # all three attempted


# -- Race Condition Edge Cases (Issue #2051 #11A) ----------------------------


class TestRaceConditionEdgeCases:
    """Edge case tests for get_or_create_sandbox race handling."""

    @pytest.mark.asyncio
    async def test_double_race_fails_gracefully(self):
        """Both create attempts hit 'already exists' — second should propagate."""
        docker = FakeProvider("docker")
        mgr = _make_manager(providers={"docker": docker})
        mgr._repository.find_active_by_name.side_effect = [
            None,  # First check
            _make_metadata(sandbox_id="sb-stale"),  # Stale cleanup lookup
        ]

        async def _always_conflict(**kwargs):
            raise ValueError("Active sandbox with name 'test' already exists for user user-1")

        with (
            patch.object(mgr, "create_sandbox", side_effect=_always_conflict),
            pytest.raises(ValueError, match="already exists"),
        ):
            # Second create also raises 'already exists' — should propagate
            await mgr.get_or_create_sandbox(name="test", user_id="user-1", zone_id="zone-1")

    @pytest.mark.asyncio
    async def test_stale_cleanup_db_error_propagates(self):
        """DB error during stale sandbox cleanup should propagate."""
        from sqlalchemy.exc import SQLAlchemyError

        docker = FakeProvider("docker")
        mgr = _make_manager(providers={"docker": docker})
        mgr._repository.find_active_by_name.side_effect = [
            None,  # First check
            _make_metadata(sandbox_id="sb-stale"),  # Stale lookup
        ]

        # First create fails with name conflict
        call_count = {"n": 0}

        async def _first_fails(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ValueError("Active sandbox with name 'test' already exists for user user-1")
            return _make_metadata(sandbox_id="sb-new")

        # Make stale cleanup fail with DB error
        mgr._repository.update_metadata.side_effect = SQLAlchemyError("connection lost")

        with (
            patch.object(mgr, "create_sandbox", side_effect=_first_fails),
            pytest.raises(SQLAlchemyError, match="connection lost"),
        ):
            await mgr.get_or_create_sandbox(name="test", user_id="user-1", zone_id="zone-1")

    @pytest.mark.asyncio
    async def test_non_conflict_valueerror_propagates(self):
        """ValueError that is NOT 'already exists' should propagate immediately."""
        docker = FakeProvider("docker")
        mgr = _make_manager(providers={"docker": docker})

        async def _invalid_provider(**kwargs):
            raise ValueError("Provider 'invalid' not available")

        with (
            patch.object(mgr, "create_sandbox", side_effect=_invalid_provider),
            pytest.raises(ValueError, match="not available"),
        ):
            await mgr.get_or_create_sandbox(name="test", user_id="user-1", zone_id="zone-1")


# -- Build Default Registry Tests (Issue #2051 #9A) -------------------------


class TestBuildDefaultRegistry:
    """Tests for SandboxManager._build_default_registry.

    _build_default_registry uses lazy imports inside try/except blocks,
    so we patch at the *source* modules (not sandbox_manager) and use
    ``patch.dict("sys.modules", {module: None})`` to simulate missing packages.
    """

    _DOCKER_MOD = "nexus.bricks.sandbox.sandbox_docker_provider"
    _E2B_MOD = "nexus.bricks.sandbox.sandbox_e2b_provider"
    _MONTY_MOD = "nexus.bricks.sandbox.sandbox_monty_provider"

    def test_docker_registered_when_available(self):
        """Docker provider is registered when docker package is importable."""
        with (
            patch(f"{self._DOCKER_MOD}.DockerSandboxProvider") as mock_docker_cls,
            patch.dict("sys.modules", {self._MONTY_MOD: None}),
        ):
            mock_docker_cls.return_value = MagicMock(spec=SandboxProvider)
            registry = SandboxManager._build_default_registry(
                e2b_api_key=None,
                e2b_team_id=None,
                e2b_template_id=None,
                config=None,
            )

        assert registry.has("docker")

    def test_docker_skipped_on_import_error(self):
        """Docker provider gracefully skipped when docker not installed."""
        with patch.dict(
            "sys.modules",
            {
                self._DOCKER_MOD: None,
                self._MONTY_MOD: None,
            },
        ):
            registry = SandboxManager._build_default_registry(
                e2b_api_key=None,
                e2b_team_id=None,
                e2b_template_id=None,
                config=None,
            )

        assert not registry.has("docker")

    def test_docker_skipped_on_runtime_error(self):
        """Docker provider skipped when Docker daemon not running."""
        with (
            patch(
                f"{self._DOCKER_MOD}.DockerSandboxProvider",
                side_effect=RuntimeError("Docker not running"),
            ),
            patch.dict("sys.modules", {self._MONTY_MOD: None}),
        ):
            registry = SandboxManager._build_default_registry(
                e2b_api_key=None,
                e2b_team_id=None,
                e2b_template_id=None,
                config=None,
            )

        assert not registry.has("docker")

    def test_e2b_registered_when_key_provided(self):
        """E2B provider registered when API key is provided."""
        with (
            patch.dict(
                "sys.modules",
                {
                    self._DOCKER_MOD: None,
                    self._MONTY_MOD: None,
                },
            ),
            patch(f"{self._E2B_MOD}.E2BSandboxProvider") as mock_e2b_cls,
        ):
            mock_e2b_cls.return_value = MagicMock(spec=SandboxProvider)
            registry = SandboxManager._build_default_registry(
                e2b_api_key="test-key",
                e2b_team_id="team-1",
                e2b_template_id="tmpl-1",
                config=None,
            )

        assert registry.has("e2b")

    def test_e2b_skipped_without_key(self):
        """E2B provider not registered when no API key."""
        with patch.dict(
            "sys.modules",
            {
                self._DOCKER_MOD: None,
                self._MONTY_MOD: None,
            },
        ):
            registry = SandboxManager._build_default_registry(
                e2b_api_key=None,
                e2b_team_id=None,
                e2b_template_id=None,
                config=None,
            )

        assert not registry.has("e2b")
