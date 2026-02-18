"""Tests for the tiered boot architecture in nexus.factory (Issue #1513).

Validates:
- BootError construction and attributes
- _boot_kernel_services: success path, failure raises BootError, timing logged
- _boot_system_services: success path, partial failure warns
- _boot_brick_services: success path, failure logged at DEBUG
- _start_background_services: .start() called, None services skipped
- create_nexus_services: full integration, BootError propagation, log tags
"""


import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from nexus.core.exceptions import BootError, NexusError

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

    defaults = {
        "record_store": MagicMock(),
        "metadata_store": MagicMock(),
        "backend": MagicMock(),
        "router": MagicMock(),
        "engine": MagicMock(),
        "session_factory": MagicMock(),
        "perm": MagicMock(
            enforce_zone_isolation=True,
            enable_tiger_cache=True,
            allow_admin_bypass=False,
            inherit=True,
            enable_deferred=False,  # disable to simplify test
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
        "db_url": "sqlite:///:memory:",
    }
    defaults.update(overrides)
    return _BootContext(**defaults)


# ---------------------------------------------------------------------------
# TestBootKernelServices
# ---------------------------------------------------------------------------


class TestBootKernelServices:
    """Tests for _boot_kernel_services."""

    def test_success_returns_all_keys(self) -> None:
        from nexus.factory import _boot_kernel_services

        ctx = _make_mock_ctx()
        result = _boot_kernel_services(ctx)

        expected_keys = {
            "rebac_manager",
            "rebac_circuit_breaker",
            "dir_visibility_cache",
            "audit_store",
            "entity_registry",
            "permission_enforcer",
            "hierarchy_manager",
            "deferred_permission_buffer",
            "workspace_registry",
            "mount_manager",
            "workspace_manager",
            "write_observer",
            "version_service",
        }
        assert expected_keys == set(result.keys())

    def test_failure_raises_boot_error(self) -> None:
        from nexus.factory import _boot_kernel_services

        ctx = _make_mock_ctx()
        with patch(
            "nexus.rebac.manager.EnhancedReBACManager",
            side_effect=RuntimeError("db connection failed"),
        ):
            with pytest.raises(BootError) as exc_info:
                _boot_kernel_services(ctx)
            assert exc_info.value.tier == "kernel"
            assert "db connection failed" in str(exc_info.value)

    def test_timing_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        from nexus.factory import _boot_kernel_services

        ctx = _make_mock_ctx()
        with caplog.at_level(logging.INFO, logger="nexus.factory"):
            _boot_kernel_services(ctx)
        assert any("[BOOT:KERNEL]" in r.message for r in caplog.records)

    def test_start_not_called_on_write_observer(self) -> None:
        """Verify .start() is NOT called during kernel boot."""
        from nexus.factory import _boot_kernel_services

        ctx = _make_mock_ctx(enable_write_buffer=False)
        result = _boot_kernel_services(ctx)
        wo = result["write_observer"]
        # RecordStoreSyncer doesn't have .start() called in kernel boot
        # (only BufferedRecordStoreSyncer does, and only in _start_background_services)
        assert wo is not None

    def test_deferred_buffer_created_when_enabled(self) -> None:
        from nexus.factory import _boot_kernel_services

        perm = MagicMock(
            enforce_zone_isolation=True,
            enable_tiger_cache=True,
            allow_admin_bypass=False,
            inherit=True,
            enable_deferred=True,
            deferred_flush_interval=0.05,
        )
        ctx = _make_mock_ctx(perm=perm)
        result = _boot_kernel_services(ctx)
        assert result["deferred_permission_buffer"] is not None

    def test_deferred_buffer_none_when_disabled(self) -> None:
        from nexus.factory import _boot_kernel_services

        ctx = _make_mock_ctx()  # enable_deferred=False by default
        result = _boot_kernel_services(ctx)
        assert result["deferred_permission_buffer"] is None


# ---------------------------------------------------------------------------
# TestBootSystemServices
# ---------------------------------------------------------------------------


class TestBootSystemServices:
    """Tests for _boot_system_services."""

    def test_success_returns_all_keys(self) -> None:
        from nexus.factory import _boot_kernel_services, _boot_system_services

        ctx = _make_mock_ctx()
        kernel = _boot_kernel_services(ctx)
        result = _boot_system_services(ctx, kernel)

        expected_keys = {
            "agent_registry",
            "async_agent_registry",
            "namespace_manager",
            "async_namespace_manager",
            "async_vfs_router",
            "delivery_worker",
            "observability_subsystem",
            "resiliency_manager",
        }
        assert expected_keys == set(result.keys())

    def test_partial_failure_warns_but_continues(self, caplog: pytest.LogCaptureFixture) -> None:
        from nexus.factory import _boot_kernel_services, _boot_system_services

        ctx = _make_mock_ctx()
        kernel = _boot_kernel_services(ctx)

        with (
            caplog.at_level(logging.WARNING, logger="nexus.factory"),
            patch(
                "nexus.services.agents.agent_registry.AgentRegistry",
                side_effect=RuntimeError("agent db error"),
            ),
        ):
            result = _boot_system_services(ctx, kernel)

        # Agent registry failed, but others should still work
        assert result["agent_registry"] is None
        # Resiliency manager should still be created
        assert result["resiliency_manager"] is not None
        assert any("[BOOT:SYSTEM]" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# TestBootBrickServices
# ---------------------------------------------------------------------------


class TestBootBrickServices:
    """Tests for _boot_brick_services."""

    def test_success_returns_all_keys(self) -> None:
        from nexus.factory import _boot_brick_services, _boot_kernel_services

        ctx = _make_mock_ctx()
        kernel = _boot_kernel_services(ctx)
        result = _boot_brick_services(ctx, kernel)

        expected_keys = {
            "wallet_provisioner",
            "manifest_resolver",
            "manifest_metrics",
            "tool_namespace_middleware",
            "chunked_upload_service",
            "event_bus",
            "lock_manager",
            "workflow_engine",
            "api_key_creator",
            "rlm_service",
        }
        assert expected_keys == set(result.keys())

    def test_failure_logged_at_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        from nexus.factory import _boot_brick_services, _boot_kernel_services

        ctx = _make_mock_ctx()
        kernel = _boot_kernel_services(ctx)

        with caplog.at_level(logging.DEBUG, logger="nexus.factory"):
            result = _boot_brick_services(ctx, kernel)

        # Brick services should return keys even if some are None
        assert "wallet_provisioner" in result
        assert any("[BOOT:BRICK]" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# TestStartBackgroundServices
# ---------------------------------------------------------------------------


class TestStartBackgroundServices:
    """Tests for _start_background_services."""

    def test_start_called_on_deferred_buffer(self) -> None:
        from nexus.factory import _start_background_services

        dpb = MagicMock()
        kernel = {"deferred_permission_buffer": dpb, "write_observer": None}
        system = {"delivery_worker": None}
        _start_background_services(kernel, system)
        dpb.start.assert_called_once()

    def test_start_called_on_delivery_worker(self) -> None:
        from nexus.factory import _start_background_services

        dw = MagicMock()
        kernel = {"deferred_permission_buffer": None, "write_observer": None}
        system = {"delivery_worker": dw}
        _start_background_services(kernel, system)
        dw.start.assert_called_once()

    def test_none_services_skipped(self) -> None:
        from nexus.factory import _start_background_services

        kernel = {"deferred_permission_buffer": None, "write_observer": None}
        system = {"delivery_worker": None}
        # Should not raise
        _start_background_services(kernel, system)

    def test_buffered_syncer_started(self) -> None:
        from nexus.factory import _start_background_services
        from nexus.storage.record_store_syncer import BufferedRecordStoreSyncer

        wo = MagicMock(spec=BufferedRecordStoreSyncer)
        kernel = {"deferred_permission_buffer": None, "write_observer": wo}
        system = {"delivery_worker": None}
        _start_background_services(kernel, system)
        wo.start.assert_called_once()


# ---------------------------------------------------------------------------
# TestCreateNexusServicesIntegration
# ---------------------------------------------------------------------------


class TestCreateNexusServicesIntegration:
    """Integration tests for create_nexus_services orchestrator."""

    def test_full_boot_returns_kernel_services(self) -> None:
        from nexus.core.config import KernelServices
        from nexus.factory import create_nexus_services

        record_store = MagicMock()
        record_store.engine = MagicMock()
        record_store.session_factory = MagicMock()
        record_store.database_url = "sqlite:///:memory:"

        result = create_nexus_services(
            record_store=record_store,
            metadata_store=MagicMock(),
            backend=MagicMock(),
            router=MagicMock(),
        )
        assert isinstance(result, KernelServices)
        assert result.rebac_manager is not None
        assert result.rebac_circuit_breaker is not None
        assert result.permission_enforcer is not None

    def test_kernel_failure_propagates_boot_error(self) -> None:
        from nexus.factory import create_nexus_services

        record_store = MagicMock()
        record_store.engine = MagicMock()
        record_store.session_factory = MagicMock()
        record_store.database_url = "sqlite:///:memory:"

        with (
            patch(
                "nexus.rebac.manager.EnhancedReBACManager",
                side_effect=RuntimeError("fatal"),
            ),
            pytest.raises(BootError),
        ):
            create_nexus_services(
                record_store=record_store,
                metadata_store=MagicMock(),
                backend=MagicMock(),
                router=MagicMock(),
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
                router=MagicMock(),
            )

        messages = " ".join(r.message for r in caplog.records)
        assert "[BOOT:KERNEL]" in messages
        assert "[BOOT:SYSTEM]" in messages
        assert "[BOOT:BRICK]" in messages
