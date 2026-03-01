"""Memory Protocol isolation tests with fakes (Issue #2190, Decision 10A).

Verifies that Memory brick classes (MemoryStateManager, MemoryVersioning)
work correctly when given fake Protocol implementations instead of concrete
ReBAC classes. This proves the Protocol decoupling is real, not just
annotation-level.
"""

from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

from nexus.contracts.types import OperationContext, Permission

# ── Fakes ──────────────────────────────────────────────────────────────


class FakePermissionChecker:
    """Fake that satisfies MemoryPermissionCheckerProtocol."""

    def __init__(self, *, allow: bool = True) -> None:
        self._allow = allow
        self.calls: list[tuple[str, Permission]] = []

    def check_memory(self, memory: object, permission: Permission, context: object) -> bool:
        memory_id = getattr(memory, "memory_id", str(memory))
        self.calls.append((memory_id, permission))
        return self._allow


class FakeEntityResolver:
    """Fake that satisfies EntityResolverProtocol."""

    def __init__(self, ids: dict[str, str] | None = None) -> None:
        self._ids = ids or {}

    def extract_ids_from_path_parts(self, parts: list[str]) -> dict[str, str]:
        return self._ids


# ── Protocol satisfaction tests ────────────────────────────────────────


class TestFakesSatisfyProtocols:
    """Verify our fakes structurally satisfy the Protocols."""

    def test_fake_permission_checker_satisfies_protocol(self) -> None:
        from nexus.contracts.protocols.memory import MemoryPermissionCheckerProtocol

        fake = FakePermissionChecker()
        assert isinstance(fake, MemoryPermissionCheckerProtocol)

    def test_fake_entity_resolver_satisfies_protocol(self) -> None:
        from nexus.contracts.protocols.memory import EntityResolverProtocol

        fake = FakeEntityResolver()
        assert isinstance(fake, EntityResolverProtocol)


# ── MemoryStateManager isolation tests ─────────────────────────────────


class TestMemoryStateManagerWithFakes:
    """Test MemoryStateManager with fake Protocol implementations."""

    def _make_state_manager(
        self, *, allow_permission: bool = True
    ) -> tuple[Any, FakePermissionChecker, MagicMock]:
        from nexus.bricks.memory.state import MemoryStateManager

        fake_perm = FakePermissionChecker(allow=allow_permission)
        mock_router = MagicMock()
        context = OperationContext(user_id="test-user", groups=[])

        mgr = MemoryStateManager(
            memory_router=mock_router,
            permission_enforcer=fake_perm,
            context=context,
        )
        return mgr, fake_perm, mock_router

    def test_approve_delegates_to_router(self) -> None:
        mgr, fake_perm, mock_router = self._make_state_manager()
        mock_memory = MagicMock()
        mock_memory.memory_id = "mem_1"
        mock_router.get_memory_by_id.return_value = mock_memory
        mock_router.approve_memory.return_value = mock_memory

        result = mgr.approve("mem_1")

        assert result is True
        mock_router.approve_memory.assert_called_once_with("mem_1")
        assert len(fake_perm.calls) == 1
        assert fake_perm.calls[0][1] == Permission.WRITE

    def test_approve_denied_by_permission(self) -> None:
        mgr, fake_perm, mock_router = self._make_state_manager(allow_permission=False)
        mock_memory = MagicMock()
        mock_router.get_memory_by_id.return_value = mock_memory

        result = mgr.approve("mem_1")

        assert result is False
        mock_router.approve_memory.assert_not_called()

    def test_delete_delegates_to_router(self) -> None:
        mgr, _, mock_router = self._make_state_manager()
        mock_memory = MagicMock()
        mock_router.get_memory_by_id.return_value = mock_memory
        mock_router.delete_memory.return_value = True

        result = mgr.delete("mem_1")

        assert result is True
        mock_router.delete_memory.assert_called_once_with("mem_1")

    def test_delete_not_found(self) -> None:
        mgr, _, mock_router = self._make_state_manager()
        mock_router.get_memory_by_id.return_value = None

        result = mgr.delete("nonexistent")

        assert result is False

    def test_invalidate_uses_current_time_by_default(self) -> None:
        mgr, _, mock_router = self._make_state_manager()
        mock_memory = MagicMock()
        mock_router.get_memory_by_id.return_value = mock_memory
        mock_router.invalidate_memory.return_value = mock_memory

        result = mgr.invalidate("mem_1")

        assert result is True
        call_args = mock_router.invalidate_memory.call_args
        assert call_args[0][0] == "mem_1"
        # Second arg is the datetime
        ts = call_args[0][1]
        assert isinstance(ts, datetime)
        assert ts.tzinfo is not None  # timezone-aware

    def test_deactivate_delegates_to_router(self) -> None:
        mgr, _, mock_router = self._make_state_manager()
        mock_memory = MagicMock()
        mock_router.get_memory_by_id.return_value = mock_memory
        mock_router.deactivate_memory.return_value = mock_memory

        result = mgr.deactivate("mem_1")

        assert result is True

    def test_revalidate_delegates_to_router(self) -> None:
        mgr, _, mock_router = self._make_state_manager()
        mock_memory = MagicMock()
        mock_router.get_memory_by_id.return_value = mock_memory
        mock_router.revalidate_memory.return_value = mock_memory

        result = mgr.revalidate("mem_1")

        assert result is True

    def test_batch_approve(self) -> None:
        mgr, _, mock_router = self._make_state_manager()
        mock_memory = MagicMock()
        mock_router.get_memory_by_id.return_value = mock_memory
        mock_router.approve_memory.return_value = mock_memory

        result = mgr.approve_batch(["m1", "m2", "m3"])

        assert result["approved"] == 3
        assert result["failed"] == 0
        assert result["approved_ids"] == ["m1", "m2", "m3"]

    def test_batch_with_mixed_results(self) -> None:
        mgr, _, mock_router = self._make_state_manager()

        # First call succeeds, second not found
        mock_memory = MagicMock()
        mock_router.get_memory_by_id.side_effect = [mock_memory, None]
        mock_router.delete_memory.return_value = True

        result = mgr.delete_batch(["m1", "m2"])

        assert result["deleted"] == 1
        assert result["failed"] == 1
        assert result["deleted_ids"] == ["m1"]
        assert result["failed_ids"] == ["m2"]
