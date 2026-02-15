"""Protocol conformance tests for all TaskStore implementations.

Each test class is parameterized across backends so that every store
gets identical coverage.  Tests are written TDD-first: they must FAIL
until the corresponding implementations are complete.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from nexus.a2a.models import (
    Artifact,
    DataPart,
    Message,
    Task,
    TaskState,
    TaskStatus,
    TextPart,
)
from nexus.a2a.task_store import TaskStoreProtocol

# ======================================================================
# Fakes / helpers
# ======================================================================


class InMemoryStorageDriver:
    """Minimal fake for IPCStorageDriver used by VFSTaskStore tests."""

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
            # Check if the directory itself exists
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
        self._dirs.add((path.rstrip("/"), zone_id))

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
        from nexus.a2a.stores.in_memory import InMemoryTaskStore

        return InMemoryTaskStore()
    elif request.param == "vfs":
        from nexus.a2a.stores.vfs import VFSTaskStore

        driver = InMemoryStorageDriver()
        return VFSTaskStore(storage=driver)
    else:
        pytest.skip(f"Unknown backend: {request.param}")


# ======================================================================
# Save + Get
# ======================================================================


class TestSaveAndGet:
    @pytest.mark.asyncio
    async def test_save_and_get_roundtrip(self, store: TaskStoreProtocol) -> None:
        task = _make_task("t1")
        await store.save(task, zone_id="z1")

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
        await store.save(task, zone_id="z1", agent_id="agent:alice")

        loaded = await store.get("t-agent", zone_id="z1")
        assert loaded is not None
        assert loaded.id == "t-agent"

    @pytest.mark.asyncio
    async def test_save_overwrites_existing(self, store: TaskStoreProtocol) -> None:
        task1 = _make_task("t-up", state=TaskState.SUBMITTED)
        await store.save(task1, zone_id="z1")

        task2 = _make_task("t-up", state=TaskState.WORKING, text="updated")
        await store.save(task2, zone_id="z1")

        loaded = await store.get("t-up", zone_id="z1")
        assert loaded is not None
        assert loaded.status.state == TaskState.WORKING
        assert loaded.history[0].parts[0].text == "updated"  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_roundtrip_preserves_artifacts(self, store: TaskStoreProtocol) -> None:
        task = _make_task_with_artifact("t-art")
        await store.save(task, zone_id="z1")

        loaded = await store.get("t-art", zone_id="z1")
        assert loaded is not None
        assert len(loaded.artifacts) == 1
        assert loaded.artifacts[0].artifactId == "art-1"
        assert loaded.artifacts[0].name == "result.json"

    @pytest.mark.asyncio
    async def test_roundtrip_preserves_metadata(self, store: TaskStoreProtocol) -> None:
        task = _make_task("t-meta", metadata={"priority": "high", "tags": [1, 2]})
        await store.save(task, zone_id="z1")

        loaded = await store.get("t-meta", zone_id="z1")
        assert loaded is not None
        assert loaded.metadata == {"priority": "high", "tags": [1, 2]}

    @pytest.mark.asyncio
    async def test_roundtrip_preserves_context_id(self, store: TaskStoreProtocol) -> None:
        task = _make_task("t-ctx", context_id="my-context-123")
        await store.save(task, zone_id="z1")

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
        await store.save(task, zone_id="alpha")

        result = await store.get("t-zone", zone_id="beta")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_excludes_other_zone(self, store: TaskStoreProtocol) -> None:
        await store.save(_make_task("t-a"), zone_id="alpha")
        await store.save(_make_task("t-b"), zone_id="beta")

        alpha_tasks = await store.list_tasks(zone_id="alpha")
        assert len(alpha_tasks) == 1
        assert alpha_tasks[0].id == "t-a"

        beta_tasks = await store.list_tasks(zone_id="beta")
        assert len(beta_tasks) == 1
        assert beta_tasks[0].id == "t-b"

    @pytest.mark.asyncio
    async def test_delete_wrong_zone_returns_false(self, store: TaskStoreProtocol) -> None:
        await store.save(_make_task("t-del"), zone_id="alpha")

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

        await store.save(task_a, zone_id="alpha")
        await store.save(task_b, zone_id="beta")

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
        await store.save(_make_task("t-del"), zone_id="z1")

        result = await store.delete("t-del", zone_id="z1")
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_removes_task(self, store: TaskStoreProtocol) -> None:
        await store.save(_make_task("t-del2"), zone_id="z1")
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
            await store.save(_make_task(f"t-{i}"), zone_id="z1")
            # Small delay to ensure distinct timestamps for ordering
            await asyncio.sleep(0.01)

        result = await store.list_tasks(zone_id="z1")
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_list_filter_by_state(self, store: TaskStoreProtocol) -> None:
        await store.save(_make_task("t-sub", state=TaskState.SUBMITTED), zone_id="z1")
        await store.save(_make_task("t-work", state=TaskState.WORKING), zone_id="z1")
        await store.save(_make_task("t-done", state=TaskState.COMPLETED), zone_id="z1")

        working = await store.list_tasks(zone_id="z1", state=TaskState.WORKING)
        assert len(working) == 1
        assert working[0].id == "t-work"

    @pytest.mark.asyncio
    async def test_list_filter_by_agent_id(self, store: TaskStoreProtocol) -> None:
        await store.save(_make_task("t-alice"), zone_id="z1", agent_id="agent:alice")
        await store.save(_make_task("t-bob"), zone_id="z1", agent_id="agent:bob")

        alice_tasks = await store.list_tasks(zone_id="z1", agent_id="agent:alice")
        assert len(alice_tasks) == 1
        assert alice_tasks[0].id == "t-alice"

    @pytest.mark.asyncio
    async def test_list_pagination_limit(self, store: TaskStoreProtocol) -> None:
        for i in range(5):
            await store.save(_make_task(f"t-{i}"), zone_id="z1")
            await asyncio.sleep(0.01)

        result = await store.list_tasks(zone_id="z1", limit=2)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_list_pagination_offset(self, store: TaskStoreProtocol) -> None:
        for i in range(5):
            await store.save(_make_task(f"t-{i}"), zone_id="z1")
            await asyncio.sleep(0.01)

        all_tasks = await store.list_tasks(zone_id="z1", limit=100)
        offset_tasks = await store.list_tasks(zone_id="z1", offset=2, limit=100)
        assert len(offset_tasks) == 3
        assert offset_tasks[0].id == all_tasks[2].id

    @pytest.mark.asyncio
    async def test_list_ordered_newest_first(self, store: TaskStoreProtocol) -> None:
        for i in range(3):
            await store.save(_make_task(f"t-{i}"), zone_id="z1")
            await asyncio.sleep(0.02)

        result = await store.list_tasks(zone_id="z1")
        assert len(result) == 3
        # Newest first — t-2 was created last
        assert result[0].id == "t-2"
        assert result[2].id == "t-0"
