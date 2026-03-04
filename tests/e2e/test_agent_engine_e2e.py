"""E2E test for Agent Engine Phase 1 — real LLM + real NexusFS.

Tests the full agent lifecycle: spawn → resume → tool calls → checkpoint.
Requires ANTHROPIC_API_KEY or OPENAI_API_KEY in the environment or ~/koi/.env.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Load API keys from ~/koi/.env if not already in env
# ---------------------------------------------------------------------------
_KOI_ENV = Path.home() / "koi" / ".env"
if _KOI_ENV.exists():
    for line in _KOI_ENV.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'")
            if key and key not in os.environ:
                os.environ[key] = value


def _pick_model_and_key() -> tuple[str, str]:
    """Return (model_name, api_key) from available env vars."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "claude-haiku-4-5-20251001", os.environ["ANTHROPIC_API_KEY"]
    if os.environ.get("OPENAI_API_KEY"):
        return "gpt-4o-mini", os.environ["OPENAI_API_KEY"]
    pytest.skip("No ANTHROPIC_API_KEY or OPENAI_API_KEY found")
    return ("", "")  # unreachable


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def model_and_key():
    return _pick_model_and_key()


@pytest.fixture
def nexus_fs_direct(tmp_path):
    """Minimal NexusFS instance (no HTTP server)."""
    os.environ.setdefault("NEXUS_JWT_SECRET", "test-agent-engine-e2e")

    from nexus.backends.local import LocalBackend
    from nexus.core.config import PermissionConfig
    from nexus.factory import create_nexus_fs
    from nexus.storage.raft_metadata_store import RaftMetadataStore
    from nexus.storage.record_store import SQLAlchemyRecordStore

    storage = tmp_path / "storage"
    storage.mkdir()
    backend = LocalBackend(root_path=str(storage))
    meta = RaftMetadataStore.embedded(str(tmp_path / "raft"))
    rec = SQLAlchemyRecordStore()  # in-memory SQLite

    nx = create_nexus_fs(
        backend=backend,
        metadata_store=meta,
        record_store=rec,
        permissions=PermissionConfig(enforce=False),
    )
    yield nx
    nx.close()
    rec.close()


@pytest.fixture
def llm_provider(model_and_key):
    """Real LiteLLMProvider backed by Anthropic or OpenAI."""
    model, api_key = model_and_key

    from pydantic import SecretStr

    from nexus.bricks.llm.config import LLMConfig
    from nexus.bricks.llm.provider import LiteLLMProvider

    config = LLMConfig(
        model=model,
        api_key=SecretStr(api_key),
        temperature=0.0,
        max_output_tokens=512,
        timeout=60.0,
        num_retries=1,
        drop_params=True,
        native_tool_calling=True,
    )
    provider = LiteLLMProvider(config)

    # Anthropic rejects `temperature` + `top_p` together.
    # Rebuild the async partial without top_p for Claude models.
    if "claude" in model:
        from functools import partial

        import litellm as _litellm

        provider._acompletion_partial = partial(
            _litellm.acompletion,
            model=model,
            api_key=api_key,
            temperature=0.0,
            max_completion_tokens=512,
            timeout=60.0,
            drop_params=True,
        )

    yield provider
    # Cleanup
    loop = asyncio.new_event_loop()
    loop.run_until_complete(provider.cleanup())
    loop.close()


@pytest.fixture
def process_manager(nexus_fs_direct, llm_provider):
    """ProcessManager wired to real NexusFS + real LLM."""
    from nexus.system_services.agent_runtime.process_manager import ProcessManager

    return ProcessManager(
        vfs=nexus_fs_direct,
        llm_provider=llm_provider,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_spawn_and_simple_chat(process_manager):
    """Spawn an agent, send a simple message, verify we get a text response."""
    from nexus.contracts.llm_types import Message, MessageRole
    from nexus.system_services.agent_runtime.types import (
        AgentProcessConfig,
        AgentProcessState,
        Completed,
        Error,
        TextDelta,
    )

    zone_id = f"test-zone-{uuid.uuid4().hex[:8]}"
    owner_id = "test-owner"

    # 1. Spawn
    config = AgentProcessConfig(
        name="test-chat-agent",
        model="unused",  # ProcessManager uses the injected LLM
        tools=(),  # No tools for simple chat
        max_turns=1,
    )
    proc = await process_manager.spawn(owner_id, zone_id, config=config)
    assert proc.state == AgentProcessState.CREATED
    assert proc.pid

    # 2. Resume with a simple message
    msg = Message(role=MessageRole.USER, content="What is 2 + 2? Reply with just the number.")
    events = []
    async for event in process_manager.resume(proc.pid, msg):
        events.append(event)

    # 3. Verify events
    errors = [e for e in events if isinstance(e, Error)]
    assert not errors, f"Agent errors: {[e.error for e in errors]}"

    text_deltas = [e for e in events if isinstance(e, TextDelta)]
    assert text_deltas, "Expected at least one TextDelta event"

    completed = [e for e in events if isinstance(e, Completed)]
    assert completed, "Expected Completed event"

    # The response should contain "4"
    full_text = "".join(e.text for e in text_deltas)
    assert "4" in full_text, f"Expected '4' in response, got: {full_text}"

    # 4. Process should be back to SLEEPING
    proc_after = await process_manager.get_process(proc.pid)
    assert proc_after is not None
    assert proc_after.state == AgentProcessState.SLEEPING


async def test_agent_with_read_file_tool(process_manager, nexus_fs_direct):
    """Spawn an agent with file tools, ask it to read a file."""
    from nexus.contracts.llm_types import Message, MessageRole
    from nexus.contracts.types import OperationContext
    from nexus.system_services.agent_runtime.types import (
        AgentProcessConfig,
        Error,
        TextDelta,
        ToolCallResult,
        ToolCallStart,
    )

    zone_id = f"test-zone-{uuid.uuid4().hex[:8]}"
    owner_id = "test-owner"

    # Create a test file in VFS
    ctx = OperationContext(user_id=owner_id, groups=[], zone_id=zone_id, is_system=True)
    test_content = "The secret code is NEXUS-42."
    nexus_fs_direct.sys_write(
        f"/{zone_id}/data/secret.txt",
        test_content.encode("utf-8"),
        context=ctx,
    )

    # Spawn agent with read_file tool
    config = AgentProcessConfig(
        name="test-file-reader",
        model="unused",
        tools=("read_file",),
        max_turns=3,
        system_prompt=(
            "You are a file reading assistant. When asked about file contents, "
            "use the read_file tool to read the file and report what you find."
        ),
    )
    proc = await process_manager.spawn(owner_id, zone_id, config=config)

    # Ask agent to read the file
    msg = Message(
        role=MessageRole.USER,
        content=f"Read the file at /{zone_id}/data/secret.txt and tell me the secret code.",
    )

    events = []
    async for event in process_manager.resume(proc.pid, msg):
        events.append(event)

    # Verify no errors
    errors = [e for e in events if isinstance(e, Error)]
    assert not errors, f"Agent errors: {[e.error for e in errors]}"

    # Should have tool call events (read_file)
    tool_starts = [e for e in events if isinstance(e, ToolCallStart)]
    tool_results = [e for e in events if isinstance(e, ToolCallResult)]

    # The agent should have used read_file at least once
    assert tool_starts, "Expected at least one ToolCallStart event"
    assert tool_results, "Expected at least one ToolCallResult event"

    # Verify the tool actually read the file content
    read_results = [e for e in tool_results if "NEXUS-42" in e.result]
    assert read_results, "Expected read_file to return file content with 'NEXUS-42'"

    # Final text should mention the secret code
    text_deltas = [e for e in events if isinstance(e, TextDelta)]
    full_text = "".join(e.text for e in text_deltas)
    assert "NEXUS-42" in full_text, f"Expected 'NEXUS-42' in response, got: {full_text}"


async def test_checkpoint_persists_across_resume(process_manager):
    """Verify conversation persists: first turn sets context, second turn recalls it."""
    from nexus.contracts.llm_types import Message, MessageRole
    from nexus.system_services.agent_runtime.types import (
        AgentProcessConfig,
        Error,
        TextDelta,
    )

    zone_id = f"test-zone-{uuid.uuid4().hex[:8]}"
    owner_id = "test-owner"

    config = AgentProcessConfig(
        name="test-memory-agent",
        model="unused",
        tools=(),
        max_turns=1,
    )
    proc = await process_manager.spawn(owner_id, zone_id, config=config)

    # Turn 1: Establish context
    msg1 = Message(
        role=MessageRole.USER,
        content="Remember this: my favorite color is purple. Just acknowledge.",
    )
    events1 = []
    async for event in process_manager.resume(proc.pid, msg1):
        events1.append(event)

    errors1 = [e for e in events1 if isinstance(e, Error)]
    assert not errors1, f"Turn 1 errors: {[e.error for e in errors1]}"

    # Turn 2: Recall context
    msg2 = Message(
        role=MessageRole.USER,
        content="What is my favorite color? Reply with just the color.",
    )
    events2 = []
    async for event in process_manager.resume(proc.pid, msg2):
        events2.append(event)

    errors2 = [e for e in events2 if isinstance(e, Error)]
    assert not errors2, f"Turn 2 errors: {[e.error for e in errors2]}"

    text_deltas = [e for e in events2 if isinstance(e, TextDelta)]
    full_text = "".join(e.text for e in text_deltas).lower()
    assert "purple" in full_text, f"Expected 'purple' in response, got: {full_text}"


async def test_terminate_removes_process(process_manager):
    """Verify terminate removes agent from process table."""
    from nexus.system_services.agent_runtime.types import AgentProcessConfig

    zone_id = f"test-zone-{uuid.uuid4().hex[:8]}"

    config = AgentProcessConfig(
        name="test-terminate-agent",
        model="unused",
        tools=(),
        max_turns=1,
    )
    proc = await process_manager.spawn("owner", zone_id, config=config)
    assert await process_manager.get_process(proc.pid) is not None

    await process_manager.terminate(proc.pid)
    assert await process_manager.get_process(proc.pid) is None
