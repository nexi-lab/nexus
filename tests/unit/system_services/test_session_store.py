"""TDD test scaffolding for SessionStore (Issue #2761, Phase 1).

Tests define the expected behavior of SessionStoreProtocol implementations.
Written RED-first: behavioral tests will fail until the concrete implementation
is built in system_services/agent_runtime/.

Contract under test:
    SessionStore.checkpoint()        — serialize session state to CAS
    SessionStore.restore()           — deserialize from CAS hash
    SessionStore.list_checkpoints()  — enumerate checkpoints per agent
    SessionStore.delete_checkpoint() — remove from CAS

CAS = Content-Addressable Storage (deduplication via content hash).

See: src/nexus/contracts/agent_runtime_types.py
"""

from datetime import UTC, datetime

import pytest

from nexus.contracts.agent_runtime_types import (
    CheckpointError,
    CheckpointInfo,
    CheckpointNotFoundError,
    SessionStoreProtocol,
)

# ======================================================================
# Value type tests (pass immediately)
# ======================================================================


class TestCheckpointInfo:
    """Verify CheckpointInfo frozen dataclass."""

    def test_creation(self) -> None:
        info = CheckpointInfo(
            checkpoint_hash="abc123",
            agent_id="agent-1",
            pid="p-1",
            turn_count=5,
            created_at=datetime.now(tz=UTC),
            size_bytes=1024,
        )
        assert info.checkpoint_hash == "abc123"
        assert info.turn_count == 5
        assert info.size_bytes == 1024

    def test_immutable(self) -> None:
        info = CheckpointInfo(
            checkpoint_hash="abc123",
            agent_id="agent-1",
            pid="p-1",
            turn_count=5,
            created_at=datetime.now(tz=UTC),
            size_bytes=1024,
        )
        attr = "turn_count"
        with pytest.raises(AttributeError):
            setattr(info, attr, 10)


class TestCheckpointExceptions:
    """Verify checkpoint exception types."""

    def test_checkpoint_error_base(self) -> None:
        err = CheckpointError("serialization failed")
        assert err.is_expected is False
        assert err.checkpoint_hash is None

    def test_checkpoint_not_found(self) -> None:
        err = CheckpointNotFoundError("abc123deadbeef")
        assert err.checkpoint_hash == "abc123deadbeef"
        assert err.is_expected is True
        assert err.status_code == 404
        assert "abc123deadbeef" in str(err)


# ======================================================================
# Protocol conformance
# ======================================================================


class TestProtocolConformance:
    """Verify SessionStoreProtocol structure."""

    def test_protocol_has_required_methods(self) -> None:
        # Verify the protocol defines the expected methods
        assert hasattr(SessionStoreProtocol, "checkpoint")
        assert hasattr(SessionStoreProtocol, "restore")
        assert hasattr(SessionStoreProtocol, "list_checkpoints")
        assert hasattr(SessionStoreProtocol, "delete_checkpoint")


# ======================================================================
# Behavioral tests (RED — need real implementation)
# ======================================================================


class TestSessionStoreCheckpoint:
    """Tests for checkpoint() — saving session state."""

    async def test_checkpoint_returns_hash(self) -> None:
        from nexus.system_services.agent_runtime.session_store import SessionStore

        store = SessionStore()
        session_data = {
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there"},
            ],
            "context_tokens": 150,
            "tool_state": {},
        }

        hash_ = await store.checkpoint("p-1", session_data, agent_id="agent-1")
        assert isinstance(hash_, str)
        assert len(hash_) > 0

    async def test_checkpoint_deterministic_hash(self) -> None:
        """Same session data produces same CAS hash (content-addressable)."""
        from nexus.system_services.agent_runtime.session_store import SessionStore

        store = SessionStore()
        data = {"messages": [{"role": "user", "content": "test"}]}

        hash1 = await store.checkpoint("p-1", data, agent_id="agent-1")
        hash2 = await store.checkpoint("p-1", data, agent_id="agent-1")
        assert hash1 == hash2

    async def test_checkpoint_different_data_different_hash(self) -> None:
        from nexus.system_services.agent_runtime.session_store import SessionStore

        store = SessionStore()

        hash1 = await store.checkpoint(
            "p-1",
            {"messages": [{"role": "user", "content": "hello"}]},
            agent_id="agent-1",
        )
        hash2 = await store.checkpoint(
            "p-1",
            {"messages": [{"role": "user", "content": "world"}]},
            agent_id="agent-1",
        )
        assert hash1 != hash2

    async def test_checkpoint_empty_session_data(self) -> None:
        """Empty session data is valid and produces a hash."""
        from nexus.system_services.agent_runtime.session_store import SessionStore

        store = SessionStore()
        hash_ = await store.checkpoint("p-1", {}, agent_id="agent-1")
        assert isinstance(hash_, str)
        assert len(hash_) > 0


class TestSessionStoreRestore:
    """Tests for restore() — loading session state from CAS."""

    async def test_restore_returns_original_data(self) -> None:
        from nexus.system_services.agent_runtime.session_store import SessionStore

        store = SessionStore()
        original = {
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there"},
            ],
            "context_tokens": 150,
        }

        hash_ = await store.checkpoint("p-1", original, agent_id="agent-1")
        restored = await store.restore(hash_)

        assert restored == original

    async def test_restore_nonexistent_hash_raises(self) -> None:
        from nexus.system_services.agent_runtime.session_store import SessionStore

        store = SessionStore()
        with pytest.raises(CheckpointNotFoundError):
            await store.restore("nonexistent-hash-abc123")

    async def test_restore_preserves_nested_structures(self) -> None:
        """Complex nested data round-trips through checkpoint/restore."""
        from nexus.system_services.agent_runtime.session_store import SessionStore

        store = SessionStore()
        original = {
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "tc-1", "function": {"name": "vfs_read", "arguments": "{}"}},
                        {"id": "tc-2", "function": {"name": "vfs_write", "arguments": "{}"}},
                    ],
                },
            ],
            "metadata": {"agent_id": "agent-1", "zone_id": "zone-1"},
            "tool_results": [
                {"tool_call_id": "tc-1", "output": "data"},
                {"tool_call_id": "tc-2", "output": "ok"},
            ],
        }

        hash_ = await store.checkpoint("p-1", original, agent_id="agent-1")
        restored = await store.restore(hash_)
        assert restored == original


class TestSessionStoreListCheckpoints:
    """Tests for list_checkpoints() — enumerating agent checkpoints."""

    async def test_list_empty(self) -> None:
        from nexus.system_services.agent_runtime.session_store import SessionStore

        store = SessionStore()
        checkpoints = await store.list_checkpoints("agent-unknown")
        assert checkpoints == []

    async def test_list_returns_checkpoint_info(self) -> None:
        from nexus.system_services.agent_runtime.session_store import SessionStore

        store = SessionStore()

        # Create multiple checkpoints for same agent
        await store.checkpoint("p-1", {"turn": 1}, agent_id="agent-1")
        await store.checkpoint("p-1", {"turn": 2}, agent_id="agent-1")
        await store.checkpoint("p-1", {"turn": 3}, agent_id="agent-1")

        checkpoints = await store.list_checkpoints("agent-1")
        assert len(checkpoints) == 3

        for cp in checkpoints:
            assert isinstance(cp, CheckpointInfo)
            assert cp.agent_id == "agent-1"
            assert cp.pid == "p-1"
            assert cp.created_at is not None
            assert cp.size_bytes > 0

    async def test_list_ordered_newest_first(self) -> None:
        from nexus.system_services.agent_runtime.session_store import SessionStore

        store = SessionStore()

        await store.checkpoint("p-1", {"turn": 1}, agent_id="agent-1")
        await store.checkpoint("p-1", {"turn": 2}, agent_id="agent-1")

        checkpoints = await store.list_checkpoints("agent-1")
        assert checkpoints[0].created_at >= checkpoints[1].created_at

    async def test_list_respects_limit(self) -> None:
        from nexus.system_services.agent_runtime.session_store import SessionStore

        store = SessionStore()

        for i in range(10):
            await store.checkpoint("p-1", {"turn": i}, agent_id="agent-1")

        checkpoints = await store.list_checkpoints("agent-1", limit=3)
        assert len(checkpoints) == 3

    async def test_list_isolates_by_agent(self) -> None:
        """Checkpoints for agent-1 are not visible to agent-2."""
        from nexus.system_services.agent_runtime.session_store import SessionStore

        store = SessionStore()

        await store.checkpoint("p-1", {"data": "a"}, agent_id="agent-1")
        await store.checkpoint("p-2", {"data": "b"}, agent_id="agent-2")

        a1_checkpoints = await store.list_checkpoints("agent-1")
        a2_checkpoints = await store.list_checkpoints("agent-2")

        assert len(a1_checkpoints) == 1
        assert len(a2_checkpoints) == 1
        assert a1_checkpoints[0].agent_id == "agent-1"
        assert a2_checkpoints[0].agent_id == "agent-2"


class TestSessionStoreDeleteCheckpoint:
    """Tests for delete_checkpoint() — removing from CAS."""

    async def test_delete_existing_returns_true(self) -> None:
        from nexus.system_services.agent_runtime.session_store import SessionStore

        store = SessionStore()
        hash_ = await store.checkpoint("p-1", {"data": "test"}, agent_id="agent-1")

        result = await store.delete_checkpoint(hash_)
        assert result is True

    async def test_delete_nonexistent_returns_false(self) -> None:
        from nexus.system_services.agent_runtime.session_store import SessionStore

        store = SessionStore()
        result = await store.delete_checkpoint("nonexistent-hash")
        assert result is False

    async def test_deleted_checkpoint_cannot_be_restored(self) -> None:
        from nexus.system_services.agent_runtime.session_store import SessionStore

        store = SessionStore()
        hash_ = await store.checkpoint("p-1", {"data": "test"}, agent_id="agent-1")
        await store.delete_checkpoint(hash_)

        with pytest.raises(CheckpointNotFoundError):
            await store.restore(hash_)

    async def test_deleted_checkpoint_removed_from_list(self) -> None:
        from nexus.system_services.agent_runtime.session_store import SessionStore

        store = SessionStore()
        hash_ = await store.checkpoint("p-1", {"data": "test"}, agent_id="agent-1")
        await store.delete_checkpoint(hash_)

        checkpoints = await store.list_checkpoints("agent-1")
        assert len(checkpoints) == 0


class TestSessionStoreEdgeCases:
    """Edge case and robustness tests."""

    async def test_large_session_data(self) -> None:
        """Large session data (simulating long conversation) checkpoints correctly."""
        from nexus.system_services.agent_runtime.session_store import SessionStore

        store = SessionStore()
        large_data = {
            "messages": [
                {"role": "user", "content": f"Message {i}: {'x' * 1000}"} for i in range(100)
            ],
        }

        hash_ = await store.checkpoint("p-1", large_data, agent_id="agent-1")
        restored = await store.restore(hash_)
        assert len(restored["messages"]) == 100

    async def test_concurrent_checkpoints(self) -> None:
        """Multiple concurrent checkpoints for different agents succeed."""
        import asyncio

        from nexus.system_services.agent_runtime.session_store import SessionStore

        store = SessionStore()

        async def checkpoint_agent(agent_id: str) -> str:
            return await store.checkpoint(
                f"p-{agent_id}",
                {"agent": agent_id, "data": "test"},
                agent_id=agent_id,
            )

        hashes = await asyncio.gather(*(checkpoint_agent(f"agent-{i}") for i in range(10)))

        # All hashes should be unique (different content)
        assert len(set(hashes)) == 10

    async def test_special_characters_in_data(self) -> None:
        """Session data with special characters round-trips correctly."""
        from nexus.system_services.agent_runtime.session_store import SessionStore

        store = SessionStore()
        data = {
            "messages": [
                {"role": "user", "content": "こんにちは世界 🌍"},
                {"role": "assistant", "content": "Hello! \n\t\r\0"},
            ],
        }

        hash_ = await store.checkpoint("p-1", data, agent_id="agent-1")
        restored = await store.restore(hash_)
        assert restored == data
