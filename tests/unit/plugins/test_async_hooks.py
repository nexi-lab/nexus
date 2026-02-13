"""Tests for AsyncHookEngine adapter (Issue #1440)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nexus.plugins.async_hooks import AsyncHookEngine, _phase_to_hook_type
from nexus.plugins.hooks import HookType, PluginHooks
from nexus.services.protocols.hook_engine import (
    HookContext,
    HookEngineProtocol,
    HookId,
    HookResult,
    HookSpec,
)
from tests.unit.core.protocols.test_conformance import assert_protocol_conformance


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def inner() -> PluginHooks:
    """Real PluginHooks instance (lightweight, no I/O)."""
    return PluginHooks()


@pytest.fixture()
def engine(inner: PluginHooks) -> AsyncHookEngine:
    return AsyncHookEngine(inner)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestConformance:
    def test_assert_protocol_conformance(self) -> None:
        assert_protocol_conformance(AsyncHookEngine, HookEngineProtocol)

    def test_isinstance_check(self, engine: AsyncHookEngine) -> None:
        assert isinstance(engine, HookEngineProtocol)


# ---------------------------------------------------------------------------
# Phase name translation
# ---------------------------------------------------------------------------


class TestPhaseTranslation:
    @pytest.mark.parametrize(
        ("phase", "expected"),
        [
            ("pre_read", HookType.BEFORE_READ),
            ("post_read", HookType.AFTER_READ),
            ("pre_write", HookType.BEFORE_WRITE),
            ("post_write", HookType.AFTER_WRITE),
            ("pre_delete", HookType.BEFORE_DELETE),
            ("post_delete", HookType.AFTER_DELETE),
            ("pre_mkdir", HookType.BEFORE_MKDIR),
            ("post_mkdir", HookType.AFTER_MKDIR),
            ("pre_copy", HookType.BEFORE_COPY),
            ("post_copy", HookType.AFTER_COPY),
        ],
    )
    def test_known_phases(self, phase: str, expected: HookType) -> None:
        assert _phase_to_hook_type(phase) == expected

    def test_unknown_phase_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown hook phase"):
            _phase_to_hook_type("unknown_phase")


# ---------------------------------------------------------------------------
# Register / Unregister roundtrip
# ---------------------------------------------------------------------------


class TestRegisterUnregister:
    @pytest.mark.asyncio()
    async def test_register_returns_hook_id(self, engine: AsyncHookEngine) -> None:
        handler = AsyncMock(return_value=HookResult(proceed=True, modified_context=None, error=None))
        spec = HookSpec(phase="pre_write", handler_name="test_hook", priority=10)
        hook_id = await engine.register_hook(spec, handler)
        assert isinstance(hook_id, HookId)
        assert hook_id.id  # non-empty

    @pytest.mark.asyncio()
    async def test_unregister_known_hook(self, engine: AsyncHookEngine) -> None:
        handler = AsyncMock(return_value=HookResult(proceed=True, modified_context=None, error=None))
        spec = HookSpec(phase="pre_write", handler_name="test_hook")
        hook_id = await engine.register_hook(spec, handler)
        assert await engine.unregister_hook(hook_id) is True

    @pytest.mark.asyncio()
    async def test_unregister_unknown_hook(self, engine: AsyncHookEngine) -> None:
        assert await engine.unregister_hook(HookId(id="nonexistent")) is False

    @pytest.mark.asyncio()
    async def test_double_unregister(self, engine: AsyncHookEngine) -> None:
        handler = AsyncMock(return_value=HookResult(proceed=True, modified_context=None, error=None))
        spec = HookSpec(phase="post_read", handler_name="h1")
        hook_id = await engine.register_hook(spec, handler)
        assert await engine.unregister_hook(hook_id) is True
        assert await engine.unregister_hook(hook_id) is False


# ---------------------------------------------------------------------------
# Fire
# ---------------------------------------------------------------------------


class TestFire:
    @pytest.mark.asyncio()
    async def test_fire_with_no_handlers(self, engine: AsyncHookEngine) -> None:
        ctx = HookContext(
            phase="pre_write", path="/workspace/file.txt",
            zone_id="z1", agent_id="a1", payload={"size": 100},
        )
        result = await engine.fire("pre_write", ctx)
        assert isinstance(result, HookResult)
        assert result.proceed is True

    @pytest.mark.asyncio()
    async def test_fire_handler_receives_context(self, engine: AsyncHookEngine) -> None:
        captured: list[HookContext] = []

        async def handler(ctx: HookContext) -> HookResult:
            captured.append(ctx)
            return HookResult(proceed=True, modified_context=None, error=None)

        spec = HookSpec(phase="pre_write", handler_name="capture")
        await engine.register_hook(spec, handler)

        fire_ctx = HookContext(
            phase="pre_write", path="/workspace/f.txt",
            zone_id="z1", agent_id=None, payload={"key": "val"},
        )
        await engine.fire("pre_write", fire_ctx)

        assert len(captured) == 1
        assert captured[0].path == "/workspace/f.txt"
        assert captured[0].payload["key"] == "val"

    @pytest.mark.asyncio()
    async def test_fire_handler_vetoes(self, engine: AsyncHookEngine) -> None:
        async def veto_handler(_ctx: HookContext) -> HookResult:
            return HookResult(proceed=False, modified_context=None, error="blocked")

        spec = HookSpec(phase="pre_delete", handler_name="veto")
        await engine.register_hook(spec, veto_handler)

        ctx = HookContext(
            phase="pre_delete", path="/workspace/secret",
            zone_id=None, agent_id=None, payload={},
        )
        result = await engine.fire("pre_delete", ctx)
        assert result.proceed is False

    @pytest.mark.asyncio()
    async def test_fire_handler_modifies_context(self, engine: AsyncHookEngine) -> None:
        async def modify_handler(ctx: HookContext) -> HookResult:
            return HookResult(
                proceed=True,
                modified_context={"path": ctx.path, "injected": True},
                error=None,
            )

        spec = HookSpec(phase="post_write", handler_name="modify")
        await engine.register_hook(spec, modify_handler)

        ctx = HookContext(
            phase="post_write", path="/workspace/out.txt",
            zone_id=None, agent_id=None, payload={},
        )
        result = await engine.fire("post_write", ctx)
        assert result.proceed is True
        assert result.modified_context is not None
        assert result.modified_context["injected"] is True

    @pytest.mark.asyncio()
    async def test_fire_unknown_phase_raises(self, engine: AsyncHookEngine) -> None:
        ctx = HookContext(
            phase="unknown", path=None,
            zone_id=None, agent_id=None, payload={},
        )
        with pytest.raises(ValueError, match="Unknown hook phase"):
            await engine.fire("unknown", ctx)
