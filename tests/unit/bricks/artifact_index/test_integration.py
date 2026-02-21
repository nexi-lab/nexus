"""Integration test: real AsyncHookEngine + handlers + mock adapters.

Validates the full chain: fire hook → dispatch to handler → extract
content → call adapter.index().
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.artifact_index.hook_handlers import (
    make_graph_hook_handler,
    make_memory_hook_handler,
    make_tool_hook_handler,
)
from nexus.services.protocols.hook_engine import (
    POST_ARTIFACT_CREATE,
    POST_ARTIFACT_UPDATE,
    HookCapabilities,
    HookContext,
    HookSpec,
)
from nexus.system_services.lifecycle.hook_engine import ScopedHookEngine
from tests.unit.bricks.artifact_index.conftest import StubArtifact, StubTextPart


def _make_inner_hook_engine() -> MagicMock:
    """Create a minimal AsyncHookEngine mock for ScopedHookEngine."""
    inner = MagicMock()
    inner.__class__.__name__ = "AsyncHookEngine"
    return inner


def _no_veto_caps() -> HookCapabilities:
    return HookCapabilities(can_veto=False, can_modify_context=False, max_timeout_ms=10000)


class TestArtifactIndexIntegration:
    """Full-chain integration: ScopedHookEngine → handlers → adapters."""

    @pytest.mark.asyncio
    async def test_fire_post_artifact_create_calls_all_adapters(self) -> None:
        engine = ScopedHookEngine(inner=_make_inner_hook_engine())

        # Create mock adapters
        memory_adapter = AsyncMock()
        tool_adapter = AsyncMock()
        graph_adapter = AsyncMock()

        # Register handlers
        for phase in (POST_ARTIFACT_CREATE, POST_ARTIFACT_UPDATE):
            await engine.register_hook(
                HookSpec(
                    phase=phase,
                    handler_name="memory",
                    capabilities=_no_veto_caps(),
                ),
                make_memory_hook_handler(memory_adapter),
            )
            await engine.register_hook(
                HookSpec(
                    phase=phase,
                    handler_name="tool",
                    capabilities=_no_veto_caps(),
                ),
                make_tool_hook_handler(tool_adapter),
            )
            await engine.register_hook(
                HookSpec(
                    phase=phase,
                    handler_name="graph",
                    capabilities=_no_veto_caps(),
                ),
                make_graph_hook_handler(graph_adapter),
            )

        # Fire post_artifact_create
        artifact = StubArtifact(
            artifactId="art-1",
            parts=[StubTextPart(text="Hello world")],
        )
        ctx = HookContext(
            phase=POST_ARTIFACT_CREATE,
            path=None,
            zone_id="z1",
            agent_id=None,
            payload={
                "artifact": artifact,
                "task_id": "t1",
                "zone_id": "z1",
            },
        )

        result = await engine.fire(POST_ARTIFACT_CREATE, ctx)

        assert result.proceed is True
        # All three adapters should have been called
        memory_adapter.index.assert_called_once()
        tool_adapter.index.assert_called_once()
        graph_adapter.index.assert_called_once()

    @pytest.mark.asyncio
    async def test_adapter_error_does_not_block_others(self) -> None:
        engine = ScopedHookEngine(inner=_make_inner_hook_engine())

        memory_adapter = AsyncMock()
        memory_adapter.index.side_effect = RuntimeError("memory down")
        tool_adapter = AsyncMock()

        for phase in (POST_ARTIFACT_CREATE,):
            await engine.register_hook(
                HookSpec(
                    phase=phase,
                    handler_name="memory",
                    capabilities=_no_veto_caps(),
                ),
                make_memory_hook_handler(memory_adapter),
            )
            await engine.register_hook(
                HookSpec(
                    phase=phase,
                    handler_name="tool",
                    capabilities=_no_veto_caps(),
                ),
                make_tool_hook_handler(tool_adapter),
            )

        artifact = StubArtifact(
            artifactId="art-2",
            parts=[StubTextPart(text="test")],
        )
        ctx = HookContext(
            phase=POST_ARTIFACT_CREATE,
            path=None,
            zone_id="z1",
            agent_id=None,
            payload={
                "artifact": artifact,
                "task_id": "t2",
                "zone_id": "z1",
            },
        )

        result = await engine.fire(POST_ARTIFACT_CREATE, ctx)

        # Should still succeed — post hooks are concurrent, errors suppressed
        assert result.proceed is True
        # Tool adapter should still have been called despite memory failure
        tool_adapter.index.assert_called_once()

    @pytest.mark.asyncio
    async def test_post_artifact_update_phase(self) -> None:
        engine = ScopedHookEngine(inner=_make_inner_hook_engine())

        adapter = AsyncMock()
        await engine.register_hook(
            HookSpec(
                phase=POST_ARTIFACT_UPDATE,
                handler_name="memory",
                capabilities=_no_veto_caps(),
            ),
            make_memory_hook_handler(adapter),
        )

        artifact = StubArtifact(
            artifactId="art-3",
            parts=[StubTextPart(text="updated")],
        )
        ctx = HookContext(
            phase=POST_ARTIFACT_UPDATE,
            path=None,
            zone_id="z1",
            agent_id=None,
            payload={
                "artifact": artifact,
                "task_id": "t3",
                "zone_id": "z1",
            },
        )

        result = await engine.fire(POST_ARTIFACT_UPDATE, ctx)
        assert result.proceed is True
        adapter.index.assert_called_once()
