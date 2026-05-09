"""Unit tests for EXEC event emission from SandboxManager.run_code (issue #4081)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.contracts.protocols.activity import EventKind, Result, set_emitter


@pytest.fixture(autouse=True)
def _restore_emitter():
    """Restore the emitter singleton after each test."""
    from nexus.contracts.protocols.activity import get_emitter

    saved = get_emitter()
    yield
    set_emitter(saved)


def _make_manager(agent_id: str | None, exit_code: int, execution_time: float = 0.215):
    """Build a SandboxManager with a stubbed repository and provider."""
    from nexus.bricks.sandbox.provider_registry import ProviderRegistry
    from nexus.bricks.sandbox.sandbox_manager import SandboxManager
    from nexus.bricks.sandbox.sandbox_provider import CodeExecutionResult

    # Stub repository that returns pre-canned metadata
    repo = MagicMock()
    repo.get_metadata.return_value = {
        "provider": "docker",
        "ttl_minutes": 10,
        "agent_id": agent_id,
    }
    repo.update_metadata.return_value = None

    # Stub provider that returns a fixed CodeExecutionResult
    provider = AsyncMock()
    provider.run_code.return_value = CodeExecutionResult(
        stdout="hello",
        stderr="",
        exit_code=exit_code,
        execution_time=execution_time,
    )

    registry = ProviderRegistry()
    registry.register("docker", provider)

    manager = SandboxManager.__new__(SandboxManager)
    manager._repository = repo
    manager._registry = registry
    manager._router = None
    manager._validation_runner = None

    return manager


@pytest.mark.asyncio
async def test_run_code_emits_exec_event_on_success() -> None:
    """SandboxManager.run_code emits EventKind.EXEC with correct fields on exit_code 0."""
    captured: list[dict] = []

    class _Capture:
        def emit(self, **kw):
            captured.append(kw)

    set_emitter(_Capture())

    manager = _make_manager(agent_id="alice", exit_code=0)
    await manager.run_code("sb-1", "bash", "echo hi")

    exec_events = [c for c in captured if c.get("kind") == EventKind.EXEC]
    assert len(exec_events) == 1
    e = exec_events[0]
    assert e["actor_agent"] == "alice"
    assert e["meta"]["cmd"] == "echo hi"
    assert e["meta"]["exit_code"] == 0
    assert e["result"] == Result.OK
    assert isinstance(e["latency_ms"], int)
    assert e["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_run_code_emits_exec_event_on_failure() -> None:
    """SandboxManager.run_code emits Result.BLOCKED when exit_code != 0."""
    captured: list[dict] = []

    class _Capture:
        def emit(self, **kw):
            captured.append(kw)

    set_emitter(_Capture())

    manager = _make_manager(agent_id="bob", exit_code=1)
    await manager.run_code("sb-2", "bash", "exit 1")

    exec_events = [c for c in captured if c.get("kind") == EventKind.EXEC]
    assert len(exec_events) == 1
    e = exec_events[0]
    assert e["actor_agent"] == "bob"
    assert e["meta"]["exit_code"] == 1
    assert e["result"] == Result.BLOCKED


@pytest.mark.asyncio
async def test_run_code_no_agent_id_still_emits() -> None:
    """EXEC event is emitted even when agent_id is None (sink can filter later)."""
    captured: list[dict] = []

    class _Capture:
        def emit(self, **kw):
            captured.append(kw)

    set_emitter(_Capture())

    manager = _make_manager(agent_id=None, exit_code=0)
    await manager.run_code("sb-3", "python", "print('hi')")

    exec_events = [c for c in captured if c.get("kind") == EventKind.EXEC]
    assert len(exec_events) == 1
    assert exec_events[0]["actor_agent"] is None


@pytest.mark.asyncio
async def test_run_code_latency_ms_from_execution_time() -> None:
    """latency_ms is derived from CodeExecutionResult.execution_time (seconds → ms)."""
    captured: list[dict] = []

    class _Capture:
        def emit(self, **kw):
            captured.append(kw)

    set_emitter(_Capture())

    # execution_time = 0.5s → 500ms
    manager = _make_manager(agent_id="carol", exit_code=0, execution_time=0.5)
    await manager.run_code("sb-4", "bash", "sleep 0.5")

    exec_events = [c for c in captured if c.get("kind") == EventKind.EXEC]
    assert len(exec_events) == 1
    assert exec_events[0]["latency_ms"] == 500


@pytest.mark.asyncio
async def test_emit_fires_on_escalation_path() -> None:
    """Verify EXEC event emitted after escalation to next tier (issue #4081).

    When EscalationNeeded is raised, the manager retries on the next tier.
    The EXEC event should still be emitted with the result from the escalated tier.
    """
    from nexus.bricks.sandbox.provider_registry import ProviderRegistry
    from nexus.bricks.sandbox.sandbox_manager import SandboxManager
    from nexus.bricks.sandbox.sandbox_provider import CodeExecutionResult, EscalationNeeded
    from nexus.bricks.sandbox.sandbox_router import SandboxRouter

    captured: list[dict] = []

    class _Capture:
        def emit(self, **kw):
            captured.append(kw)

    set_emitter(_Capture())

    # Set up: first provider (monty) raises EscalationNeeded
    # second provider (docker) returns success
    repo = MagicMock()
    repo.get_metadata.return_value = {
        "provider": "monty",
        "ttl_minutes": 10,
        "agent_id": "escalation-agent",
    }
    repo.update_metadata.return_value = None

    monty_provider = AsyncMock()
    monty_provider.run_code.side_effect = EscalationNeeded(
        reason="resource_limit", suggested_tier="docker"
    )

    docker_provider = AsyncMock()
    docker_provider.create.return_value = "temp-sb-id"
    docker_provider.run_code.return_value = CodeExecutionResult(
        stdout="escalated output",
        stderr="",
        exit_code=0,
        execution_time=0.123,
    )
    docker_provider.destroy.return_value = None

    registry = ProviderRegistry()
    registry.register("monty", monty_provider)
    registry.register("docker", docker_provider)

    manager = SandboxManager.__new__(SandboxManager)
    manager._repository = repo
    manager._registry = registry
    manager._router = SandboxRouter(
        available_providers={"monty": monty_provider, "docker": docker_provider}
    )
    manager._validation_runner = None

    # Run code, which will escalate
    await manager.run_code("sb-esc", "bash", "some code")

    # Verify EXEC event was emitted with escalated result
    exec_events = [c for c in captured if c.get("kind") == EventKind.EXEC]
    assert len(exec_events) == 1
    e = exec_events[0]
    assert e["actor_agent"] == "escalation-agent"
    assert e["result"] == Result.OK  # docker_provider returned exit_code=0
    assert e["meta"]["exit_code"] == 0
    assert e["meta"]["cmd"] == "some code"
    # 0.123s → 123ms
    assert e["latency_ms"] == 123
