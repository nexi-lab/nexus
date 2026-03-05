"""CacheStoreABC-backed task store for dev/test and embedded mode.

Tasks are serialized as JSON bytes and stored in a ``CacheStoreABC``
driver.  When backed by ``InMemoryCacheStore`` the behavior is
equivalent to the old dict-backed store; when backed by
``DragonflyCacheStore`` the tasks become distributed.

Data is ephemeral (no disk persistence) — use ``VFSTaskStore`` for
durable storage.

Cache key layout::

    a2a:task:{zone_id}:{task_id}  →  JSON { task, agent_id, created_at }
"""

import json
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from nexus.bricks.a2a.exceptions import StaleTaskVersionError
from nexus.bricks.a2a.models import Task, TaskState

_KEY_PREFIX = "a2a:task"


@runtime_checkable
class _CacheKV(Protocol):
    """Subset of CacheStoreABC used by CacheBackedTaskStore.

    Canonical CacheStoreABC now lives in nexus.contracts.cache_store.
    This protocol captures the exact contract we rely on, satisfied by
    InMemoryCacheStore, DragonflyCacheStore, and NullCacheStore.
    """

    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: bytes, ttl: int | None = None) -> None: ...
    async def delete(self, key: str) -> bool: ...
    async def keys_by_pattern(self, pattern: str) -> list[str]: ...
    async def get_many(self, keys: list[str]) -> dict[str, bytes | None]: ...


def _key(zone_id: str, task_id: str) -> str:
    return f"{_KEY_PREFIX}:{zone_id}:{task_id}"


def _zone_pattern(zone_id: str) -> str:
    return f"{_KEY_PREFIX}:{zone_id}:*"


class CacheBackedTaskStore:
    """CacheStoreABC-backed implementation of ``TaskStoreProtocol``.

    Replaces the old ``InMemoryTaskStore`` (plain dict) with proper
    Four-Pillar storage: all ephemeral KV goes through CacheStoreABC.

    Args:
        cache: Any ``CacheStoreABC`` driver (InMemoryCacheStore,
            DragonflyCacheStore, NullCacheStore, ...).
    """

    def __init__(self, cache: _CacheKV) -> None:
        self._cache = cache

    async def save(
        self,
        task: Task,
        *,
        zone_id: str,
        agent_id: str | None = None,
        expected_version: int | None = None,
    ) -> None:
        k = _key(zone_id, task.id)
        # Preserve created_at from existing record when updating
        existing_raw = await self._cache.get(k)
        if existing_raw is not None:
            existing: dict[str, Any] = json.loads(existing_raw)
            created_at: str = existing.get("created_at", datetime.now(UTC).isoformat())

            # Optimistic locking: reject if stored version differs
            if expected_version is not None:
                stored_task = existing.get("task", {})
                stored_version = stored_task.get("version", 1)
                if stored_version != expected_version:
                    raise StaleTaskVersionError(
                        message=(
                            f"Task {task.id} version mismatch: "
                            f"expected {expected_version}, found {stored_version}"
                        ),
                        data={
                            "taskId": task.id,
                            "expectedVersion": expected_version,
                            "storedVersion": stored_version,
                        },
                    )
        else:
            created_at = datetime.now(UTC).isoformat()

        record: dict[str, Any] = {
            "task": task.model_dump(mode="json"),
            "zone_id": zone_id,
            "agent_id": agent_id,
            "created_at": created_at,
        }
        await self._cache.set(k, json.dumps(record).encode())

    async def get(self, task_id: str, *, zone_id: str) -> Task | None:
        raw = await self._cache.get(_key(zone_id, task_id))
        if raw is None:
            return None
        record: dict[str, Any] = json.loads(raw)
        return Task.model_validate(record["task"])

    async def delete(self, task_id: str, *, zone_id: str) -> bool:
        return await self._cache.delete(_key(zone_id, task_id))

    async def list_tasks(
        self,
        *,
        zone_id: str,
        agent_id: str | None = None,
        state: TaskState | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        keys = await self._cache.keys_by_pattern(_zone_pattern(zone_id))
        if not keys:
            return []

        values = await self._cache.get_many(keys)

        results: list[tuple[str, Task]] = []
        for raw in values.values():
            if raw is None:
                continue
            record: dict[str, Any] = json.loads(raw)
            if agent_id is not None and record.get("agent_id") != agent_id:
                continue
            task = Task.model_validate(record["task"])
            if state is not None and task.status.state != state:
                continue
            results.append((record.get("created_at", ""), task))

        # Sort by created_at descending (newest first)
        results.sort(key=lambda pair: pair[0], reverse=True)
        tasks = [task for _, task in results]
        return tasks[offset : offset + limit]
