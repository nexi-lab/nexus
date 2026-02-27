"""Protocol conformance tests for all TaskStore implementations.

Each test class is parameterized across backends so that every store
gets identical coverage.  Restored from #1699 prune and extended for
§17.6 convergence (agent-scoped paths + MessageEnvelope format).
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from nexus.bricks.a2a.models import (
    Artifact,
    DataPart,
    Message,
    Task,
    TaskState,
    TaskStatus,
    TextPart,
)
from nexus.bricks.a2a.task_store import TaskStoreProtocol

# ======================================================================
# Fakes / helpers
# ======================================================================


class InMemoryStorageDriver:
    """Minimal fake for VFSOperations used by VFSTaskStore tests."""

    def __init__(self) -> None:
        self._files: dict[tuple[str, str], bytes] = {}
        self._dirs: set[tuple[str, str]] = set()

    async def read(self, path: str, zone_id: str) -> bytes:
        key = (path, zone_id)
        if key not in self._files:
            raise FileNotFoundError(path)
        return self._files[key]

    async def write(self, path: str, data: bytes, zone_id: str) -> None:
        self._files[(path, zone_id)] = data

    async def list_dir(self, path: str, zone_id: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        entries: set[str] = set()
        for (p, z), _ in self._files.items():
            if z == zone_id and p.startswith(prefix):
                relative = p[len(prefix) :]
                # Only direct children (no deeper slashes)
                if "/" not in relative:
                    entries.add(relative)
        for d, z in self._dirs:
            if z == zone_id and d.startswith(prefix):
                relative = d[len(prefix) :]
                if "/" not in relative.rstrip("/"):
                    entries.add(relative.rstrip("/"))
        if not entries:
            norm = path.rstrip("/")
            dir_exists = (norm, zone_id) in self._dirs
            has_children = any(p.startswith(norm + "/") and z == zone_id for (p, z) in self._files)
            if not dir_exists and not has_children:
                raise FileNotFoundError(path)
        return sorted(entries)

    async def count_dir(self, path: str, zone_id: str) -> int:
        entries = await self.list_dir(path, zone_id)
        return len(entries)

    async def rename(self, src: str, dst: str, zone_id: str) -> None:
        key = (src, zone_id)
        if key not in self._files:
            raise FileNotFoundError(src)
        self._files[(dst, zone_id)] = self._files.pop(key)

    async def mkdir(self, path: str, zone_id: str) -> None:
        # Create all parent directories (matches LocalStorageDriver parents=True)
        norm = path.rstrip("/")
        parts = norm.split("/")
        for i in range(1, len(parts) + 1):
            parent = "/".join(parts[:i])
            if parent:
                self._dirs.add((parent, zone_id))

    async def exists(self, path: str, zone_id: str) -> bool:
        if (path, zone_id) in self._files:
            return True
        norm = path.rstrip("/")
        return (norm, zone_id) in self._dirs


def _make_task(
    task_id: str = "task-001",
    state: TaskState = TaskState.SUBMITTED,
    text: str = "hello",
    context_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Task:
    """Create a minimal Task for testing."""
    return Task(
        id=task_id,
        contextId=context_id or f"ctx-{task_id}",
        status=TaskStatus(state=state, timestamp=datetime.now(UTC)),
        history=[
            Message(
                role="user",
                parts=[TextPart(text=text)],
            )
        ],
        metadata=metadata,
    )


def _make_task_with_artifact(task_id: str = "task-art") -> Task:
    """Create a Task with an artifact for testing."""
    return Task(
        id=task_id,
        contextId=f"ctx-{task_id}",
        status=TaskStatus(state=TaskState.COMPLETED, timestamp=datetime.now(UTC)),
        history=[
            Message(role="user", parts=[TextPart(text="analyze this")]),
            Message(role="agent", parts=[TextPart(text="done")]),
        ],
        artifacts=[
            Artifact(
                artifactId="art-1",
                name="result.json",
                parts=[DataPart(data={"key": "value"})],
            )
        ],
    )


# ======================================================================
# Fixtures — parameterized across backends
# ======================================================================


@pytest.fixture(params=["in_memory", "vfs"])
def store(request: pytest.FixtureRequest) -> TaskStoreProtocol:
    """Create a TaskStore instance for each backend."""
    if request.param == "in_memory":
        from nexus.bricks.a2a.stores.in_memory import CacheBackedTaskStore
        from nexus.cache.inmemory import InMemoryCacheStore

        return CacheBackedTaskStore(InMemoryCacheStore())
    elif request.param == "vfs":
        from nexus.bricks.a2a.stores.vfs import VFSTaskStore

        driver = InMemoryStorageDriver()
        return VFSTaskStore(storage=driver)
    else:
        pytest.skip(f"Unknown backend: {request.param}")


@pytest.fixture()
def vfs_store() -> tuple[Any, Any]:
    """VFSTaskStore with exposed driver for index tests."""
    from nexus.bricks.a2a.stores.vfs import VFSTaskStore

    driver = InMemoryStorageDriver()
    store = VFSTaskStore(storage=driver, max_cache_size=5)
    return store, driver


# ======================================================================
# Save + Get
# ======================================================================


class TestSaveAndGet:
    @pytest.mark.asyncio
    async def test_save_and_get_roundtrip(self, store: TaskStoreProtocol) -> None:
        task = _make_task("t1")
        await store.save(task, zone_id="z1", agent_id="agent-a")

        loaded = await store.get("t1", zone_id="z1")
        assert loaded is not None
        assert loaded.id == "t1"
        assert loaded.status.state == TaskState.SUBMITTED
        assert len(loaded.history) == 1
        assert loaded.history[0].parts[0].text == "hello"  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, store: TaskStoreProtocol) -> None:
        result = await store.get("nonexistent", zone_id="z1")
        assert result is None

    @pytest.mark.asyncio
    async def test_save_with_agent_id(self, store: TaskStoreProtocol) -> None:
        task = _make_task("t-agent")
        await store.save(task, zone_id="z1", agent_id="agent-alice")

        loaded = await store.get("t-agent", zone_id="z1")
        assert loaded is not None
        assert loaded.id == "t-agent"

    @pytest.mark.asyncio
    async def test_save_without_agent_id(self, store: TaskStoreProtocol) -> None:
        """Tasks saved without agent_id should still be retrievable."""
        task = _make_task("t-noagent")
        await store.save(task, zone_id="z1")

        loaded = await store.get("t-noagent", zone_id="z1")
        assert loaded is not None
        assert loaded.id == "t-noagent"

    @pytest.mark.asyncio
    async def test_save_overwrites_existing(self, store: TaskStoreProtocol) -> None:
        task1 = _make_task("t-up", state=TaskState.SUBMITTED)
        await store.save(task1, zone_id="z1", agent_id="agent-a")

        task2 = _make_task("t-up", state=TaskState.WORKING, text="updated")
        await store.save(task2, zone_id="z1", agent_id="agent-a")

        loaded = await store.get("t-up", zone_id="z1")
        assert loaded is not None
        assert loaded.status.state == TaskState.WORKING
        assert loaded.history[0].parts[0].text == "updated"  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_roundtrip_preserves_artifacts(self, store: TaskStoreProtocol) -> None:
        task = _make_task_with_artifact("t-art")
        await store.save(task, zone_id="z1", agent_id="agent-a")

        loaded = await store.get("t-art", zone_id="z1")
        assert loaded is not None
        assert len(loaded.artifacts) == 1
        assert loaded.artifacts[0].artifactId == "art-1"
        assert loaded.artifacts[0].name == "result.json"

    @pytest.mark.asyncio
    async def test_roundtrip_preserves_metadata(self, store: TaskStoreProtocol) -> None:
        task = _make_task("t-meta", metadata={"priority": "high", "tags": [1, 2]})
        await store.save(task, zone_id="z1", agent_id="agent-a")

        loaded = await store.get("t-meta", zone_id="z1")
        assert loaded is not None
        assert loaded.metadata == {"priority": "high", "tags": [1, 2]}

    @pytest.mark.asyncio
    async def test_roundtrip_preserves_context_id(self, store: TaskStoreProtocol) -> None:
        task = _make_task("t-ctx", context_id="my-context-123")
        await store.save(task, zone_id="z1", agent_id="agent-a")

        loaded = await store.get("t-ctx", zone_id="z1")
        assert loaded is not None
        assert loaded.contextId == "my-context-123"


# ======================================================================
# Zone Isolation (Security boundary — critical)
# ======================================================================


class TestZoneIsolation:
    @pytest.mark.asyncio
    async def test_get_wrong_zone_returns_none(self, store: TaskStoreProtocol) -> None:
        task = _make_task("t-zone")
        await store.save(task, zone_id="alpha", agent_id="agent-a")

        result = await store.get("t-zone", zone_id="beta")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_excludes_other_zone(self, store: TaskStoreProtocol) -> None:
        await store.save(_make_task("t-a"), zone_id="alpha", agent_id="agent-a")
        await store.save(_make_task("t-b"), zone_id="beta", agent_id="agent-a")

        alpha_tasks = await store.list_tasks(zone_id="alpha")
        assert len(alpha_tasks) == 1
        assert alpha_tasks[0].id == "t-a"

        beta_tasks = await store.list_tasks(zone_id="beta")
        assert len(beta_tasks) == 1
        assert beta_tasks[0].id == "t-b"

    @pytest.mark.asyncio
    async def test_delete_wrong_zone_returns_false(self, store: TaskStoreProtocol) -> None:
        await store.save(_make_task("t-del"), zone_id="alpha", agent_id="agent-a")

        result = await store.delete("t-del", zone_id="beta")
        assert result is False

        # Still exists in the correct zone
        loaded = await store.get("t-del", zone_id="alpha")
        assert loaded is not None

    @pytest.mark.asyncio
    async def test_same_task_id_different_zones(self, store: TaskStoreProtocol) -> None:
        """Same task ID can exist in different zones independently."""
        task_a = _make_task("shared-id", text="alpha version")
        task_b = _make_task("shared-id", text="beta version")

        await store.save(task_a, zone_id="alpha", agent_id="agent-a")
        await store.save(task_b, zone_id="beta", agent_id="agent-a")

        loaded_a = await store.get("shared-id", zone_id="alpha")
        loaded_b = await store.get("shared-id", zone_id="beta")

        assert loaded_a is not None
        assert loaded_b is not None
        assert loaded_a.history[0].parts[0].text == "alpha version"  # type: ignore[union-attr]
        assert loaded_b.history[0].parts[0].text == "beta version"  # type: ignore[union-attr]


# ======================================================================
# Delete
# ======================================================================


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_existing_returns_true(self, store: TaskStoreProtocol) -> None:
        await store.save(_make_task("t-del"), zone_id="z1", agent_id="agent-a")

        result = await store.delete("t-del", zone_id="z1")
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_removes_task(self, store: TaskStoreProtocol) -> None:
        await store.save(_make_task("t-del2"), zone_id="z1", agent_id="agent-a")
        await store.delete("t-del2", zone_id="z1")

        loaded = await store.get("t-del2", zone_id="z1")
        assert loaded is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(self, store: TaskStoreProtocol) -> None:
        result = await store.delete("nonexistent", zone_id="z1")
        assert result is False


# ======================================================================
# List Tasks
# ======================================================================


class TestListTasks:
    @pytest.mark.asyncio
    async def test_list_empty(self, store: TaskStoreProtocol) -> None:
        result = await store.list_tasks(zone_id="empty-zone")
        assert result == []

    @pytest.mark.asyncio
    async def test_list_returns_all_in_zone(self, store: TaskStoreProtocol) -> None:
        for i in range(3):
            await store.save(_make_task(f"t-{i}"), zone_id="z1", agent_id="agent-a")
            await asyncio.sleep(0.01)

        result = await store.list_tasks(zone_id="z1")
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_list_filter_by_state(self, store: TaskStoreProtocol) -> None:
        await store.save(
            _make_task("t-sub", state=TaskState.SUBMITTED),
            zone_id="z1",
            agent_id="agent-a",
        )
        await store.save(
            _make_task("t-work", state=TaskState.WORKING),
            zone_id="z1",
            agent_id="agent-a",
        )
        await store.save(
            _make_task("t-done", state=TaskState.COMPLETED),
            zone_id="z1",
            agent_id="agent-a",
        )

        working = await store.list_tasks(zone_id="z1", state=TaskState.WORKING)
        assert len(working) == 1
        assert working[0].id == "t-work"

    @pytest.mark.asyncio
    async def test_list_filter_by_agent_id(self, store: TaskStoreProtocol) -> None:
        await store.save(_make_task("t-alice"), zone_id="z1", agent_id="agent-alice")
        await store.save(_make_task("t-bob"), zone_id="z1", agent_id="agent-bob")

        alice_tasks = await store.list_tasks(zone_id="z1", agent_id="agent-alice")
        assert len(alice_tasks) == 1
        assert alice_tasks[0].id == "t-alice"

    @pytest.mark.asyncio
    async def test_list_pagination_limit(self, store: TaskStoreProtocol) -> None:
        for i in range(5):
            await store.save(_make_task(f"t-{i}"), zone_id="z1", agent_id="agent-a")
            await asyncio.sleep(0.01)

        result = await store.list_tasks(zone_id="z1", limit=2)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_list_pagination_offset(self, store: TaskStoreProtocol) -> None:
        for i in range(5):
            await store.save(_make_task(f"t-{i}"), zone_id="z1", agent_id="agent-a")
            await asyncio.sleep(0.01)

        all_tasks = await store.list_tasks(zone_id="z1", limit=100)
        offset_tasks = await store.list_tasks(zone_id="z1", offset=2, limit=100)
        assert len(offset_tasks) == 3
        assert offset_tasks[0].id == all_tasks[2].id

    @pytest.mark.asyncio
    async def test_list_ordered_newest_first(self, store: TaskStoreProtocol) -> None:
        for i in range(3):
            await store.save(_make_task(f"t-{i}"), zone_id="z1", agent_id="agent-a")
            await asyncio.sleep(0.02)

        result = await store.list_tasks(zone_id="z1")
        assert len(result) == 3
        # Newest first — t-2 was created last
        assert result[0].id == "t-2"
        assert result[2].id == "t-0"


# ======================================================================
# VFS Task Index (VFSTaskStore-specific)
# ======================================================================


class TestVFSTaskIndex:
    """Tests for the task_id → agent_id LRU index in VFSTaskStore."""

    @pytest.mark.asyncio
    async def test_cache_hit_avoids_scan(self, vfs_store: tuple[Any, Any]) -> None:
        store, _driver = vfs_store
        task = _make_task("t-idx")
        await store.save(task, zone_id="z1", agent_id="agent-a")

        # Second get should be a cache hit (no scan logged)
        loaded = await store.get("t-idx", zone_id="z1")
        assert loaded is not None
        assert loaded.id == "t-idx"

    @pytest.mark.asyncio
    async def test_cold_start_scan_finds_task(self, vfs_store: tuple[Any, Any]) -> None:
        store, _driver = vfs_store
        task = _make_task("t-cold")
        await store.save(task, zone_id="z1", agent_id="agent-a")

        # Clear the index to simulate cold start
        store._task_index.clear()
        store._locks.clear()

        loaded = await store.get("t-cold", zone_id="z1")
        assert loaded is not None
        assert loaded.id == "t-cold"

    @pytest.mark.asyncio
    async def test_lru_eviction(self, vfs_store: tuple[Any, Any]) -> None:
        store, _driver = vfs_store
        # max_cache_size=5, save 7 tasks
        for i in range(7):
            await store.save(
                _make_task(f"t-{i}"),
                zone_id="z1",
                agent_id="agent-a",
            )
            await asyncio.sleep(0.01)

        # First 2 should have been evicted
        assert len(store._task_index) == 5

        # Evicted task should still be findable via scan
        loaded = await store.get("t-0", zone_id="z1")
        assert loaded is not None
        assert loaded.id == "t-0"

    @pytest.mark.asyncio
    async def test_delete_clears_index(self, vfs_store: tuple[Any, Any]) -> None:
        store, _driver = vfs_store
        await store.save(_make_task("t-rm"), zone_id="z1", agent_id="agent-a")
        assert "t-rm" in store._task_index

        await store.delete("t-rm", zone_id="z1")
        assert "t-rm" not in store._task_index

    @pytest.mark.asyncio
    async def test_concurrent_saves_both_indexed(self, vfs_store: tuple[Any, Any]) -> None:
        store, _driver = vfs_store

        async def save_task(task_id: str, agent_id: str) -> None:
            await store.save(_make_task(task_id), zone_id="z1", agent_id=agent_id)

        await asyncio.gather(
            save_task("t-1", "agent-a"),
            save_task("t-2", "agent-b"),
        )

        assert "t-1" in store._task_index
        assert "t-2" in store._task_index

    @pytest.mark.asyncio
    async def test_cross_agent_tasks_listed_zone_wide(
        self,
        vfs_store: tuple[Any, Any],
    ) -> None:
        store, _driver = vfs_store
        await store.save(_make_task("t-a"), zone_id="z1", agent_id="agent-a")
        await store.save(_make_task("t-b"), zone_id="z1", agent_id="agent-b")

        all_tasks = await store.list_tasks(zone_id="z1")
        assert len(all_tasks) == 2
        task_ids = {t.id for t in all_tasks}
        assert task_ids == {"t-a", "t-b"}


# ======================================================================
# MessageEnvelope format verification (VFSTaskStore-specific)
# ======================================================================


class TestVFSEnvelopeFormat:
    """Verify tasks are stored as MessageEnvelope on disk."""

    @pytest.mark.asyncio
    async def test_stored_as_message_envelope(self, vfs_store: tuple[Any, Any]) -> None:
        from nexus.bricks.ipc.envelope import MessageEnvelope

        store, driver = vfs_store
        task = _make_task("t-env")
        await store.save(task, zone_id="z1", agent_id="agent-a")

        # Find the stored file
        files = [(p, z) for (p, z) in driver._files if "t-env" in p and z == "z1"]
        assert len(files) == 1
        path, zone = files[0]
        data = driver._files[(path, zone)]

        # Verify it's a valid MessageEnvelope
        envelope = MessageEnvelope.from_bytes(data)
        assert envelope.type.value == "task"
        assert envelope.sender == "a2a_gateway"
        assert envelope.recipient == "agent-a"
        assert envelope.correlation_id == "t-env"

    @pytest.mark.asyncio
    async def test_task_payload_roundtrips(self, vfs_store: tuple[Any, Any]) -> None:
        store, _driver = vfs_store
        original = _make_task_with_artifact("t-roundtrip")
        await store.save(original, zone_id="z1", agent_id="agent-a")

        loaded = await store.get("t-roundtrip", zone_id="z1")
        assert loaded is not None
        assert loaded.id == original.id
        assert loaded.status.state == original.status.state
        assert len(loaded.artifacts) == 1
        assert loaded.artifacts[0].artifactId == "art-1"
