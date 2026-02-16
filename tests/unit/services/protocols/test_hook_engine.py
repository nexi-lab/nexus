"""Tests for HookEngineProtocol and hook data models (Issue #1383)."""

from __future__ import annotations

import dataclasses

import pytest

from nexus.services.protocols.hook_engine import (
    POST_COPY,
    POST_DELETE,
    POST_MKDIR,
    POST_READ,
    POST_WRITE,
    PRE_COPY,
    PRE_DELETE,
    PRE_MKDIR,
    PRE_READ,
    PRE_WRITE,
    HookContext,
    HookEngineProtocol,
    HookId,
    HookResult,
    HookSpec,
)

# ---------------------------------------------------------------------------
# HookId frozen dataclass tests
# ---------------------------------------------------------------------------


class TestHookId:
    def test_frozen(self) -> None:
        hid = HookId(id="h1")
        with pytest.raises(dataclasses.FrozenInstanceError):
            hid.id = "changed"  # type: ignore[misc]

    def test_slots(self) -> None:
        assert hasattr(HookId, "__slots__")


# ---------------------------------------------------------------------------
# HookSpec frozen dataclass tests
# ---------------------------------------------------------------------------


class TestHookSpec:
    def test_frozen(self) -> None:
        spec = HookSpec(phase=PRE_WRITE, handler_name="validator", priority=10)
        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.phase = "changed"  # type: ignore[misc]

    def test_slots(self) -> None:
        assert hasattr(HookSpec, "__slots__")

    def test_default_priority(self) -> None:
        spec = HookSpec(phase=PRE_READ, handler_name="h")
        assert spec.priority == 0

    def test_fields(self) -> None:
        fields = {f.name for f in dataclasses.fields(HookSpec)}
        assert fields == {"phase", "handler_name", "priority"}


# ---------------------------------------------------------------------------
# HookContext frozen dataclass tests
# ---------------------------------------------------------------------------


class TestHookContext:
    def test_frozen(self) -> None:
        ctx = HookContext(
            phase=PRE_WRITE,
            path="/ws/f.txt",
            zone_id="z1",
            agent_id="a1",
            payload={},
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.phase = "changed"  # type: ignore[misc]

    def test_slots(self) -> None:
        assert hasattr(HookContext, "__slots__")

    def test_all_none_optional(self) -> None:
        ctx = HookContext(
            phase=POST_READ,
            path=None,
            zone_id=None,
            agent_id=None,
            payload={},
        )
        assert ctx.path is None
        assert ctx.zone_id is None
        assert ctx.agent_id is None

    def test_fields(self) -> None:
        fields = {f.name for f in dataclasses.fields(HookContext)}
        assert fields == {"phase", "path", "zone_id", "agent_id", "payload"}


# ---------------------------------------------------------------------------
# HookResult frozen dataclass tests
# ---------------------------------------------------------------------------


class TestHookResult:
    def test_frozen(self) -> None:
        result = HookResult(proceed=True, modified_context=None, error=None)
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.proceed = False  # type: ignore[misc]

    def test_slots(self) -> None:
        assert hasattr(HookResult, "__slots__")

    def test_proceed_true(self) -> None:
        result = HookResult(proceed=True, modified_context={"key": "val"}, error=None)
        assert result.proceed is True
        assert result.modified_context == {"key": "val"}

    def test_proceed_false_with_error(self) -> None:
        result = HookResult(proceed=False, modified_context=None, error="denied")
        assert result.proceed is False
        assert result.error == "denied"

    def test_fields(self) -> None:
        fields = {f.name for f in dataclasses.fields(HookResult)}
        assert fields == {"proceed", "modified_context", "error"}


# ---------------------------------------------------------------------------
# Phase constants
# ---------------------------------------------------------------------------


class TestPhaseConstants:
    def test_phase_values(self) -> None:
        assert PRE_READ == "pre_read"
        assert POST_READ == "post_read"
        assert PRE_WRITE == "pre_write"
        assert POST_WRITE == "post_write"
        assert PRE_DELETE == "pre_delete"
        assert POST_DELETE == "post_delete"
        assert PRE_MKDIR == "pre_mkdir"
        assert POST_MKDIR == "post_mkdir"
        assert PRE_COPY == "pre_copy"
        assert POST_COPY == "post_copy"

    def test_phases_are_strings(self) -> None:
        for phase in (
            PRE_READ,
            POST_READ,
            PRE_WRITE,
            POST_WRITE,
            PRE_DELETE,
            POST_DELETE,
            PRE_MKDIR,
            POST_MKDIR,
            PRE_COPY,
            POST_COPY,
        ):
            assert isinstance(phase, str)


# ---------------------------------------------------------------------------
# Protocol structural tests
# ---------------------------------------------------------------------------


class TestHookEngineProtocol:
    def test_expected_methods(self) -> None:
        expected = {"register_hook", "unregister_hook", "fire"}
        actual = {
            name
            for name in dir(HookEngineProtocol)
            if not name.startswith("_") and callable(getattr(HookEngineProtocol, name))
        }
        assert expected <= actual


# ---------------------------------------------------------------------------
# Conformance test against existing PluginHooks (partial)
# ---------------------------------------------------------------------------


class TestPluginHooksConformance:
    """Verify PluginHooks has analogous methods.

    Method names differ (register vs register_hook, execute vs fire),
    so we only check the existing methods exist and are callable.
    """

    def test_plugin_hooks_has_analogous_methods(self) -> None:
        from nexus.plugins.hooks import PluginHooks

        # PluginHooks uses: register, unregister, execute
        # HookEngineProtocol uses: register_hook, unregister_hook, fire
        # These are analogous but not identical names.
        assert callable(getattr(PluginHooks, "register", None))
        assert callable(getattr(PluginHooks, "unregister", None))
        assert callable(getattr(PluginHooks, "execute", None))
