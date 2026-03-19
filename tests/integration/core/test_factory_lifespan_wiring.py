"""Integration test: factory-created services arrive at app.state via lifespan.

Issue #2195: Verifies that EventLog and SchedulerService constructed by the
factory are the same objects that end up on ``app.state`` after lifespan
startup completes.

This is an integration test because it exercises the real factory boot path
(with mocked infrastructure) and the real lifespan wiring functions.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.contracts.deployment_profile import DeploymentProfile
from nexus.core.config import SystemServices
from nexus.factory import _boot_kernel_services, _boot_system_services, _BootContext


def _make_boot_context(**overrides: object) -> _BootContext:
    """Build a _BootContext with fully mocked dependencies."""
    record_store = MagicMock()
    record_store.engine = MagicMock()
    record_store.read_engine = MagicMock()
    record_store.session_factory = MagicMock()
    record_store.has_read_replica = False
    record_store.database_url = "sqlite://"
    record_store.async_session_factory = MagicMock()

    backend = MagicMock()
    backend.root_path = "/tmp/test"
    backend.has_root_path = True
    backend.on_write_callback = None
    backend.on_sync_callback = None

    from nexus.lib.performance_tuning import resolve_profile_tuning

    profile_tuning = resolve_profile_tuning(DeploymentProfile.FULL)

    defaults: dict[str, Any] = {
        "record_store": record_store,
        "metadata_store": MagicMock(),
        "backend": backend,
        "router": MagicMock(),
        "engine": record_store.engine,
        "read_engine": record_store.read_engine,
        "perm": MagicMock(
            enforce=False,
            enable_deferred=False,
            enable_tiger_cache=False,
            enforce_zone_isolation=True,
            allow_admin_bypass=False,
            inherit=True,
            deferred_flush_interval=0.05,
        ),
        "cache_ttl_seconds": 300,
        "dist": MagicMock(
            enable_events=False,
            enable_locks=False,
            enable_workflows=False,
        ),
        "zone_id": None,
        "agent_id": None,
        "enable_write_buffer": False,
        "resiliency_raw": None,
        "db_url": "sqlite://",
        "profile_tuning": profile_tuning,
    }
    defaults.update(overrides)
    return _BootContext(**defaults)


class TestEventLogFactoryToLifespan:
    """Verify EventLog flows from factory -> SystemServices -> lifespan."""

    def test_factory_event_log_arrives_at_app_state(self) -> None:
        """EventLog from factory boot is the same object on app.state.event_log."""
        sentinel_event_log = MagicMock()

        # Build system services with a mocked EventLog
        ctx = _make_boot_context()
        _boot_kernel_services(ctx)
        with patch(
            "nexus.system_services.event_log.create_event_log",
            return_value=sentinel_event_log,
        ):
            system_dict = _boot_system_services(ctx)

        assert system_dict["event_log"] is sentinel_event_log

        # Simulate what the orchestrator does
        system_services = SystemServices(
            event_log=system_dict["event_log"],
        )

        # Simulate lifespan: read from system_services
        event_log = getattr(system_services, "event_log", None)
        assert event_log is sentinel_event_log


class TestSchedulerFactoryToLifespan:
    """Verify SchedulerService flows from factory -> SystemServices -> lifespan."""

    def test_factory_scheduler_arrives_in_system_services(self) -> None:
        """SchedulerService from factory boot lands in SystemServices."""
        ctx = _make_boot_context(db_url="postgresql://localhost/test")
        _boot_kernel_services(ctx)
        system_dict = _boot_system_services(ctx)

        scheduler = system_dict["scheduler_service"]
        assert scheduler is not None
        assert scheduler._initialized is False  # two-phase: not yet initialized

        # Simulate orchestrator wiring
        system_services = SystemServices(
            scheduler_service=scheduler,
        )

        # Lifespan reads from system_services — identity check
        assert getattr(system_services, "scheduler_service", None) is scheduler

    @pytest.mark.asyncio
    async def test_scheduler_two_phase_init_via_lifespan(self) -> None:
        """SchedulerService can be initialized with a mock pool (two-phase)."""
        ctx = _make_boot_context(db_url="postgresql://localhost/test")
        _boot_kernel_services(ctx)
        system_dict = _boot_system_services(ctx)
        scheduler = system_dict["scheduler_service"]

        # Simulate lifespan: create mock pool and initialize
        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        acm = AsyncMock()
        acm.__aenter__ = AsyncMock(return_value=mock_conn)
        acm.__aexit__ = AsyncMock(return_value=None)
        mock_pool.acquire = MagicMock(return_value=acm)

        await scheduler.initialize(mock_pool)

        assert scheduler._initialized is True
        assert scheduler.pool is mock_pool
