"""Protocol compliance tests for zone lifecycle types (Issue #2061).

Verifies:
- ZoneFinalizerProtocol is runtime_checkable
- Each concrete finalizer satisfies the protocol
- ZonePhase enum values are correct
- Frozen dataclasses are immutable
"""

from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock

import pytest

from nexus.contracts.protocols.zone_lifecycle import (
    ZoneDeprovisionResult,
    ZoneFinalizerProtocol,
    ZoneLifecycleStatus,
    ZonePhase,
)
from nexus.system_services.lifecycle.zone_finalizers.brick_drain_finalizer import (
    BrickDrainFinalizer,
)
from nexus.system_services.lifecycle.zone_finalizers.cache_finalizer import CacheZoneFinalizer
from nexus.system_services.lifecycle.zone_finalizers.mount_finalizer import MountZoneFinalizer
from nexus.system_services.lifecycle.zone_finalizers.rebac_finalizer import ReBACZoneFinalizer
from nexus.system_services.lifecycle.zone_finalizers.search_finalizer import SearchZoneFinalizer


class TestZonePhaseEnum:
    """ZonePhase StrEnum correctness."""

    def test_active_value(self):
        assert ZonePhase.ACTIVE == "Active"

    def test_terminating_value(self):
        assert ZonePhase.TERMINATING == "Terminating"

    def test_terminated_value(self):
        assert ZonePhase.TERMINATED == "Terminated"

    def test_is_string(self):
        """StrEnum values work as plain strings."""
        assert isinstance(ZonePhase.ACTIVE, str)
        assert f"phase={ZonePhase.ACTIVE}" == "phase=Active"


class TestZoneFinalizerProtocol:
    """Protocol is runtime_checkable and satisfied by concrete finalizers."""

    def test_protocol_is_runtime_checkable(self):
        assert hasattr(ZoneFinalizerProtocol, "__protocol_attrs__") or hasattr(
            ZoneFinalizerProtocol, "__abstractmethods__"
        )

    def test_cache_finalizer_satisfies_protocol(self):
        f = CacheZoneFinalizer(file_cache=MagicMock())
        assert isinstance(f, ZoneFinalizerProtocol)

    def test_search_finalizer_satisfies_protocol(self):
        f = SearchZoneFinalizer(session_factory=MagicMock())
        assert isinstance(f, ZoneFinalizerProtocol)

    def test_mount_finalizer_satisfies_protocol(self):
        f = MountZoneFinalizer(mount_service=MagicMock())
        assert isinstance(f, ZoneFinalizerProtocol)

    def test_rebac_finalizer_satisfies_protocol(self):
        f = ReBACZoneFinalizer(session_factory=MagicMock())
        assert isinstance(f, ZoneFinalizerProtocol)

    def test_brick_drain_finalizer_satisfies_protocol(self):
        f = BrickDrainFinalizer(brick_lifecycle_manager=MagicMock())
        assert isinstance(f, ZoneFinalizerProtocol)

    def test_plain_object_does_not_satisfy_protocol(self):
        assert not isinstance(object(), ZoneFinalizerProtocol)


class TestFrozenDataclasses:
    """ZoneLifecycleStatus and ZoneDeprovisionResult are immutable."""

    def test_lifecycle_status_is_frozen(self):
        status = ZoneLifecycleStatus(
            zone_id="z1",
            phase=ZonePhase.ACTIVE,
            finalizers=(),
            errors={},
        )
        with pytest.raises(FrozenInstanceError):
            status.zone_id = "z2"  # type: ignore[misc]

    def test_deprovision_result_is_frozen(self):
        result = ZoneDeprovisionResult(
            zone_id="z1",
            phase=ZonePhase.TERMINATED,
            finalizers_completed=("a",),
            finalizers_pending=(),
            finalizers_failed={},
        )
        with pytest.raises(FrozenInstanceError):
            result.zone_id = "z2"  # type: ignore[misc]

    def test_lifecycle_status_slots(self):
        status = ZoneLifecycleStatus(
            zone_id="z1",
            phase=ZonePhase.ACTIVE,
            finalizers=(),
            errors={},
        )
        assert hasattr(status, "__slots__")

    def test_deprovision_result_slots(self):
        result = ZoneDeprovisionResult(
            zone_id="z1",
            phase=ZonePhase.TERMINATED,
            finalizers_completed=(),
            finalizers_pending=(),
            finalizers_failed={},
        )
        assert hasattr(result, "__slots__")
