"""Tests for the tiered boot architecture in nexus.factory (Issue #1513).

Validates:
- BootError construction and attributes
- _boot_kernel_services: validates Storage Pillars
- _boot_system_services: critical (BootError) + degradable (WARNING + None)
- _boot_brick_services: success path, failure logged at DEBUG
- _start_background_services: .start() called, None services skipped
- create_nexus_services: full integration, BootError propagation, log tags

Issue #2193: Former kernel services moved to system tier.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from nexus.contracts.deployment_profile import DeploymentProfile
from nexus.contracts.exceptions import BootError, NexusError

# ---------------------------------------------------------------------------
# TestBootError
# ---------------------------------------------------------------------------


class TestBootError:
    """Tests for BootError exception class."""

    def test_construction_defaults(self) -> None:
        err = BootError("something broke")
        assert str(err) == "something broke"
        assert err.tier == "kernel"
        assert err.service_name == ""

    def test_construction_with_kwargs(self) -> None:
        err = BootError("db down", tier="system", service_name="rebac_manager")
        assert err.tier == "system"
        assert err.service_name == "rebac_manager"

    def test_inherits_nexus_error(self) -> None:
        err = BootError("fail")
        assert isinstance(err, NexusError)
        assert isinstance(err, Exception)

    def test_is_not_expected(self) -> None:
        err = BootError("fail")
        assert err.is_expected is False


# ---------------------------------------------------------------------------
# Helpers: minimal mock boot context
# ---------------------------------------------------------------------------


def _make_mock_ctx(**overrides: Any) -> Any:
    """Build a minimal _BootContext-like object for tier function tests."""
    from nexus.factory import _BootContext

    mock_engine = MagicMock()
    defaults = {
        "record_store": MagicMock(),
        "metadata_store": MagicMock(),
        "backend": MagicMock(),
        "dlc": MagicMock(),
        "engine": mock_engine,
        "read_engine": mock_engine,
        "perm": MagicMock(
            enforce_zone_isolation=True,
            enable_tiger_cache=True,
            allow_admin_bypass=False,
            inherit=True,
            enable_deferred=False,  # disable to simplify test
            deferred_flush_interval=0.05,
            audit_strict_mode=True,
        ),
        "audit": MagicMock(strict_mode=True),
        "cache_ttl_seconds": 300,
        "dist": MagicMock(
            enable_events=False,
            enable_workflows=False,
        ),
        "zone_id": None,
        "agent_id": None,
        "enable_write_buffer": False,
        "resiliency_raw": None,
        "db_url": "sqlite:///:memory:",
        "profile_tuning": DeploymentProfile.FULL.tuning(),
    }
    defaults.update(overrides)
    return _BootContext(**defaults)


# ---------------------------------------------------------------------------
# TestBootKernelServices
# ---------------------------------------------------------------------------


class TestBootKernelServices:
    """Tests for _boot_kernel_services (Issue #2193: validation-only)."""

    def test_returns_empty_dict(self) -> None:
        """Kernel tier validates Storage Pillars and returns empty dict."""
        from nexus.factory import _boot_kernel_services

        ctx = _make_mock_ctx()
        result = _boot_kernel_services(ctx)

        assert isinstance(result, dict)
        assert len(result) == 0

    def test_failure_on_none_dlc(self) -> None:
        """BootError when dlc is None."""
        from nexus.factory import _boot_kernel_services

        ctx = _make_mock_ctx(dlc=None)
        with pytest.raises(BootError) as exc_info:
            _boot_kernel_services(ctx)
        assert exc_info.value.tier == "kernel"

    def test_failure_on_none_metadata_store(self) -> None:
        """BootError when metadata_store is None."""
        from nexus.factory import _boot_kernel_services

        ctx = _make_mock_ctx(metadata_store=None)
        with pytest.raises(BootError) as exc_info:
            _boot_kernel_services(ctx)
        assert exc_info.value.tier == "kernel"

    def test_timing_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        from nexus.factory import _boot_kernel_services

        ctx = _make_mock_ctx()
        with caplog.at_level(logging.INFO, logger="nexus.factory._kernel"):
            _boot_kernel_services(ctx)
        assert any("[BOOT:KERNEL]" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# TestBootSystemServices
# ---------------------------------------------------------------------------


class TestBootSystemServices:
    """Tests for _boot_system_services (Issue #2193: critical + degradable)."""

    def test_success_returns_all_keys(self) -> None:
        """System tier returns dict with critical + degradable + original keys."""
        from nexus.factory import _boot_system_services

        ctx = _make_mock_ctx()
        result = _boot_system_services(ctx)

        expected_keys = {
            # Former-kernel critical
            "rebac_manager",
            "audit_store",
            "entity_registry",
            "permission_enforcer",
            "write_observer",
            # Former-kernel degradable
            # dir_visibility_cache, hierarchy_manager, namespace_manager
            # now internalized into ReBACManager — not in result dict.
            "deferred_permission_buffer",
            "workspace_registry",
            "mount_manager",
            "workspace_manager",
            # Original services
            "async_namespace_manager",
            "delivery_worker",
            "observability_subsystem",
            "resiliency_manager",
            "context_branch_service",
            "zone_lifecycle",
            # (PipeManager + AgentRegistry are kernel-internal §4.2)
            "scheduler_service",
            # Issue #3193: shared notification signal
            "event_signal",
        }
        assert expected_keys == set(result.keys())

    def test_critical_failure_raises_boot_error(self) -> None:
        """Critical service failure (rebac_manager) raises BootError."""
        from nexus.factory import _boot_system_services

        ctx = _make_mock_ctx()
        with patch(
            "nexus.bricks.rebac.manager.ReBACManager",
            side_effect=RuntimeError("db connection failed"),
        ):
            with pytest.raises(BootError) as exc_info:
                _boot_system_services(ctx)
            assert "system-critical" in exc_info.value.tier
            assert "db connection failed" in str(exc_info.value)

    def test_degradable_failure_warns_but_continues(self, caplog: pytest.LogCaptureFixture) -> None:
        from nexus.factory import _boot_system_services

        ctx = _make_mock_ctx()

        with (
            caplog.at_level(logging.WARNING, logger="nexus.factory"),
            patch(
                "nexus.bricks.rebac.manager.ReBACManager.create_namespace_manager",
                side_effect=RuntimeError("namespace db error"),
            ),
        ):
            result = _boot_system_services(ctx)

        # Namespace manager failed (internalized into rebac), async wrapper is None
        assert result["async_namespace_manager"] is None
        # Critical services should still be created
        assert result["rebac_manager"] is not None
        assert result["permission_enforcer"] is not None

    def test_critical_services_are_not_none(self) -> None:
        """All 5 critical services must be non-None on success."""
        from nexus.factory import _boot_system_services

        ctx = _make_mock_ctx()
        result = _boot_system_services(ctx)

        for key in (
            "rebac_manager",
            "audit_store",
            "entity_registry",
            "permission_enforcer",
            "write_observer",
        ):
            assert result[key] is not None, f"Critical service {key} should not be None"


# ---------------------------------------------------------------------------
# TestBootBrickServices
# ---------------------------------------------------------------------------


class TestBootBrickServices:
    """Tests for _boot_brick_services."""

    def test_success_returns_all_keys(self) -> None:
        from nexus.factory import _boot_brick_services, _boot_system_services

        ctx = _make_mock_ctx()
        system = _boot_system_services(ctx)
        result = _boot_brick_services(ctx, system)

        expected_keys = {
            "agent_event_log",
            "wallet_provisioner",
            "manifest_resolver",
            "manifest_metrics",
            "tool_namespace_middleware",
            "chunked_upload_service",
            "workflow_engine",
            "api_key_creator",
            "snapshot_service",
            "delegation_service",
            "version_service",
            "rebac_circuit_breaker",
            "governance_anomaly_service",
            "governance_collusion_service",
            "governance_graph_service",
            "governance_response_service",
            # OBSERVE-phase Zoekt observer (Issue #810)
            "zoekt_write_observer",
            # Task Manager DT_PIPE consumer
            "task_dispatch_consumer",
        }
        assert expected_keys == set(result.keys())

    def test_version_service_degrades_gracefully(self) -> None:
        """Issue #2034 / 10A: VersionService failure should not crash brick boot."""
        from nexus.factory import _boot_brick_services

        ctx = _make_mock_ctx()
        system = {
            "rebac_manager": MagicMock(),
            "entity_registry": MagicMock(),
            "acp_service": MagicMock(),
            "agent_registry": MagicMock(),
        }

        with (
            patch("nexus.factory._bricks._discover_brick_factories", return_value=[]),
            patch("nexus.factory._bricks.logger.debug") as log_debug,
            patch(
                "nexus.bricks.versioning.version_service.VersionService",
                side_effect=RuntimeError("version db unavailable"),
            ),
        ):
            result = _boot_brick_services(ctx, system, lambda _name: False)

        # version_service key exists but is None (graceful degradation)
        assert "version_service" in result
        assert result["version_service"] is None
        # Other brick services are unaffected
        assert "wallet_provisioner" in result
        assert any(
            "VersionService unavailable" in str(call.args[0])
            and "version db unavailable" in str(call.args[1])
            for call in log_debug.call_args_list
            if len(call.args) >= 2
        )

    def test_circuit_breaker_degrades_with_warning(self) -> None:
        """Issue #2034 / 14A: Circuit breaker failure logs WARNING, not fatal."""
        from nexus.factory import _boot_brick_services

        ctx = _make_mock_ctx()
        system = {
            "rebac_manager": MagicMock(),
            "entity_registry": MagicMock(),
            "acp_service": MagicMock(),
            "agent_registry": MagicMock(),
        }

        with (
            patch("nexus.factory._bricks._discover_brick_factories", return_value=[]),
            patch("nexus.factory._bricks.logger.warning") as log_warning,
            patch(
                "nexus.bricks.rebac.circuit_breaker.AsyncCircuitBreaker",
                side_effect=RuntimeError("circuit breaker config error"),
            ),
        ):
            result = _boot_brick_services(ctx, system, lambda _name: False)

        assert "rebac_circuit_breaker" in result
        assert result["rebac_circuit_breaker"] is None
        assert any(
            "circuit-breaking protection" in str(call.args[0])
            for call in log_warning.call_args_list
            if call.args
        )

    def test_failure_logged_at_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        from nexus.factory import _boot_brick_services, _boot_system_services

        ctx = _make_mock_ctx()
        system = _boot_system_services(ctx)

        with caplog.at_level(logging.DEBUG, logger="nexus.factory"):
            result = _boot_brick_services(ctx, system)

        # Brick services should return keys even if some are None
        assert "wallet_provisioner" in result
        assert any("[BOOT:BRICK]" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# TestSafeCreate (Issue #2193, Decision 8A/9A)
# ---------------------------------------------------------------------------


class TestSafeCreate:
    """Tests for _safe_create() with severity parameter."""

    def test_success_returns_instance(self) -> None:
        """Successful creation returns the factory result."""
        from nexus.factory import _safe_create

        sentinel = object()
        result = _safe_create("test_svc", lambda: sentinel, lambda _: True)
        assert result is sentinel

    def test_profile_gating_returns_none(self) -> None:
        """Service disabled by profile returns None."""
        from nexus.factory import _safe_create

        result = _safe_create("test_svc", lambda: object(), lambda _: False)
        assert result is None

    def test_debug_severity_returns_none_on_error(self) -> None:
        """Default severity (debug) logs at DEBUG and returns None."""
        from nexus.factory import _safe_create

        with patch("nexus.factory._helpers.logger.debug") as log_debug:
            result = _safe_create(
                "broken_svc",
                lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                lambda _: True,
            )
        assert result is None
        log_debug.assert_called_once()
        assert log_debug.call_args.args[0] == "[BOOT:%s] %s unavailable: %s"
        assert log_debug.call_args.args[1:3] == ("BRICK", "broken_svc")
        assert str(log_debug.call_args.args[3]) == "boom"

    def test_warning_severity_returns_none_on_error(self) -> None:
        """Warning severity logs at WARNING and returns None."""
        from nexus.factory import _safe_create

        with patch("nexus.factory._helpers.logger.warning") as log_warning:
            result = _safe_create(
                "degradable_svc",
                lambda: (_ for _ in ()).throw(RuntimeError("degraded")),
                lambda _: True,
                severity="warning",
            )
        assert result is None
        log_warning.assert_called_once()
        assert log_warning.call_args.args[0] == "[BOOT:%s] %s unavailable: %s"
        assert log_warning.call_args.args[1:3] == ("BRICK", "degradable_svc")
        assert str(log_warning.call_args.args[3]) == "degraded"

    def test_critical_severity_raises_boot_error(self) -> None:
        """Critical severity raises BootError instead of returning None."""
        from nexus.factory import _safe_create

        with pytest.raises(BootError) as exc_info:
            _safe_create(
                "critical_svc",
                lambda: (_ for _ in ()).throw(RuntimeError("fatal")),
                lambda _: True,
                severity="critical",
            )
        assert "critical_svc" in str(exc_info.value)
        assert "fatal" in str(exc_info.value)

    def test_critical_severity_success_returns_instance(self) -> None:
        """Critical severity with successful creation returns the instance."""
        from nexus.factory import _safe_create

        sentinel = object()
        result = _safe_create("critical_svc", lambda: sentinel, lambda _: True, severity="critical")
        assert result is sentinel


# ---------------------------------------------------------------------------
# TestStartBackgroundServices
# ---------------------------------------------------------------------------


class TestStartBackgroundServices:
    """Tests for _start_background_services (Issue #2193: system dict only)."""

    def test_start_called_on_deferred_buffer(self) -> None:
        """DPB start is handled by Rust kernel service_start_all() — not called here."""
        from nexus.factory import _start_background_services

        dpb = MagicMock()
        system = {
            "deferred_permission_buffer": dpb,
            "write_observer": None,
            "delivery_worker": None,
        }
        _start_background_services(system)
        dpb.start.assert_not_called()

    def test_delivery_worker_not_started_in_background(self) -> None:
        """Issue #3193: delivery worker is now async — started by coordinator, not here."""
        from nexus.factory import _start_background_services

        dw = MagicMock()
        system = {"deferred_permission_buffer": None, "write_observer": None, "delivery_worker": dw}
        _start_background_services(system)
        dw.start.assert_not_called()

    def test_none_services_skipped(self) -> None:
        from nexus.factory import _start_background_services

        system = {
            "deferred_permission_buffer": None,
            "write_observer": None,
            "delivery_worker": None,
        }
        # Should not raise
        _start_background_services(system)

    def test_zone_lifecycle_loads_terminating_zones(self) -> None:
        """Issue #2061: load_terminating_zones called on startup."""
        from nexus.factory import _start_background_services

        session_mock = MagicMock()
        sf = MagicMock()
        sf.__enter__ = MagicMock(return_value=session_mock)
        sf.__exit__ = MagicMock(return_value=False)

        zl = MagicMock()
        zl._session_factory = MagicMock(return_value=sf)

        system = {
            "deferred_permission_buffer": None,
            "write_observer": None,
            "delivery_worker": None,
            "zone_lifecycle": zl,
        }
        _start_background_services(system)
        zl.load_terminating_zones.assert_called_once_with(session_mock)


# ---------------------------------------------------------------------------
# TestCreateNexusServicesIntegration
# ---------------------------------------------------------------------------


class TestCreateNexusServicesIntegration:
    """Integration tests for create_nexus_services orchestrator."""

    def test_full_boot_returns_single_dict(self) -> None:
        from nexus.factory import create_nexus_services

        record_store = MagicMock()
        record_store.engine = MagicMock()
        record_store.session_factory = MagicMock()
        record_store.database_url = "sqlite:///:memory:"

        result = create_nexus_services(
            record_store=record_store,
            metadata_store=MagicMock(),
            backend=MagicMock(),
            dlc=MagicMock(),
        )
        assert isinstance(result, dict)
        # Issue #2193: all services in a single flat dict
        assert result["rebac_manager"] is not None
        assert result["permission_enforcer"] is not None
        assert result["rebac_circuit_breaker"] is not None

    def test_critical_failure_propagates_boot_error(self) -> None:
        from nexus.factory import create_nexus_services

        record_store = MagicMock()
        record_store.engine = MagicMock()
        record_store.session_factory = MagicMock()
        record_store.database_url = "sqlite:///:memory:"

        with (
            patch(
                "nexus.bricks.rebac.manager.ReBACManager",
                side_effect=RuntimeError("fatal"),
            ),
            pytest.raises(BootError),
        ):
            create_nexus_services(
                record_store=record_store,
                metadata_store=MagicMock(),
                backend=MagicMock(),
                dlc=MagicMock(),
            )

    def test_boot_tags_in_log_output(self, caplog: pytest.LogCaptureFixture) -> None:
        from nexus.factory import create_nexus_services

        record_store = MagicMock()
        record_store.engine = MagicMock()
        record_store.session_factory = MagicMock()
        record_store.database_url = "sqlite:///:memory:"

        with caplog.at_level(logging.DEBUG, logger="nexus.factory"):
            create_nexus_services(
                record_store=record_store,
                metadata_store=MagicMock(),
                backend=MagicMock(),
                dlc=MagicMock(),
            )

        messages = " ".join(r.message for r in caplog.records)
        assert "[BOOT:KERNEL]" in messages
        assert "[BOOT:SYSTEM]" in messages
        assert "[BOOT:BRICK]" in messages
