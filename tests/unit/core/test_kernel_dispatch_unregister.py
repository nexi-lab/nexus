"""Unit tests for KernelDispatch unregister_* methods (Issue #1452 Phase 3)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.core.nexus_fs_dispatch import DispatchMixin


class _TestDispatch(DispatchMixin):
    def __init__(self):
        from nexus_kernel import PyKernel

        self._kernel = PyKernel()
        self._init_dispatch()


@pytest.fixture()
def dispatch() -> _TestDispatch:
    return _TestDispatch()


class TestUnregisterResolver:
    def test_unregister_existing(self, dispatch: _TestDispatch) -> None:
        r = MagicMock()
        dispatch.register_resolver(r)
        assert dispatch.resolver_count == 1
        assert dispatch.unregister_resolver(r) is True
        assert dispatch.resolver_count == 0

    def test_unregister_missing(self, dispatch: _TestDispatch) -> None:
        assert dispatch.unregister_resolver(MagicMock()) is False


class TestUnregisterInterceptHooks:
    @pytest.mark.parametrize(
        "register_method,unregister_method,count_prop",
        [
            ("register_intercept_read", "unregister_intercept_read", "read_hook_count"),
            ("register_intercept_write", "unregister_intercept_write", "write_hook_count"),
            (
                "register_intercept_write_batch",
                "unregister_intercept_write_batch",
                "write_batch_hook_count",
            ),
            ("register_intercept_delete", "unregister_intercept_delete", "delete_hook_count"),
            ("register_intercept_rename", "unregister_intercept_rename", "rename_hook_count"),
            ("register_intercept_mkdir", "unregister_intercept_mkdir", "mkdir_hook_count"),
            ("register_intercept_rmdir", "unregister_intercept_rmdir", "rmdir_hook_count"),
        ],
    )
    def test_register_then_unregister(
        self,
        dispatch: _TestDispatch,
        register_method: str,
        unregister_method: str,
        count_prop: str,
    ) -> None:
        hook = MagicMock()
        getattr(dispatch, register_method)(hook)
        assert getattr(dispatch, count_prop) == 1

        result = getattr(dispatch, unregister_method)(hook)
        assert result is True
        assert getattr(dispatch, count_prop) == 0

    @pytest.mark.parametrize(
        "unregister_method",
        [
            "unregister_intercept_read",
            "unregister_intercept_write",
            "unregister_intercept_write_batch",
            "unregister_intercept_delete",
            "unregister_intercept_rename",
            "unregister_intercept_mkdir",
            "unregister_intercept_rmdir",
        ],
    )
    def test_unregister_missing_returns_false(
        self, dispatch: _TestDispatch, unregister_method: str
    ) -> None:
        assert getattr(dispatch, unregister_method)(MagicMock()) is False


class TestUnregisterObserve:
    def test_unregister_existing(self, dispatch: _TestDispatch) -> None:
        obs = MagicMock()
        dispatch.register_observe(obs)
        assert dispatch.observer_count == 1
        assert dispatch.unregister_observe(obs) is True
        assert dispatch.observer_count == 0

    def test_unregister_missing(self, dispatch: _TestDispatch) -> None:
        assert dispatch.unregister_observe(MagicMock()) is False

    async def test_multiple_observers(self, dispatch: _TestDispatch) -> None:
        from unittest.mock import AsyncMock

        from nexus.core.file_events import ALL_FILE_EVENTS, FileEvent, FileEventType

        obs1, obs2, obs3 = AsyncMock(), AsyncMock(), AsyncMock()
        obs1.event_mask = ALL_FILE_EVENTS
        obs2.event_mask = ALL_FILE_EVENTS
        obs3.event_mask = ALL_FILE_EVENTS
        dispatch.register_observe(obs1)
        dispatch.register_observe(obs2)
        dispatch.register_observe(obs3)
        assert dispatch.observer_count == 3

        dispatch.unregister_observe(obs2)
        assert dispatch.observer_count == 2
        # obs1 and obs3 remain — verify notify still reaches them
        event = FileEvent(type=FileEventType.FILE_WRITE, path="/test")
        dispatch.notify(event)
        obs1.on_mutation.assert_called_once_with(event)
        obs3.on_mutation.assert_called_once_with(event)
        obs2.on_mutation.assert_not_called()
