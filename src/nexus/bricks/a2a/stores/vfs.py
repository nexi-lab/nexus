"""VFS-backed task store — tasks as MessageEnvelopes under agent directories.

File layout::

    /agents/{agent_id}/tasks/{timestamp}_{task_id}.json

Agent-scoped paths enable unified VFS/ReBAC/EventBus access (§17.6
convergence).  Tasks are wrapped in ``MessageEnvelope`` format for
interoperability with the IPC messaging system.

Timestamp prefix enables efficient sorting by creation time without
reading file contents.  The same pattern is used by the IPC
``TTLSweeper``.
"""

import asyncio
import importlib as _il
import logging
import re
import time
from collections import OrderedDict
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from nexus.bricks.a2a.models import Task, TaskState

# Cross-brick imports via importlib to avoid a2a→ipc boundary violation
_ipc_conventions = _il.import_module("nexus.bricks.ipc.conventions")
AGENTS_ROOT = _ipc_conventions.AGENTS_ROOT
task_dead_letter_path = _ipc_conventions.task_dead_letter_path
tasks_path = _ipc_conventions.tasks_path
_ipc_envelope = _il.import_module("nexus.bricks.ipc.envelope")
MessageEnvelope = _ipc_envelope.MessageEnvelope
MessageType = _ipc_envelope.MessageType

if TYPE_CHECKING:
    from nexus.bricks.ipc.protocols import VFSOperations

logger = logging.getLogger(__name__)

_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_.@:-]+$")

# Default maximum entries in the LRU task index
_DEFAULT_MAX_CACHE_SIZE = 10_000

# Fallback agent ID when no agent is specified
_UNASSIGNED_AGENT = "_unassigned"


class _IndexEntry:
    """Lightweight cache entry mapping task_id to its location."""

    __slots__ = ("zone_id", "agent_id", "filename")

    def __init__(self, zone_id: str, agent_id: str, filename: str) -> None:
        self.zone_id = zone_id
        self.agent_id = agent_id
        self.filename = filename


class VFSTaskStore:
    """Stores A2A tasks as ``MessageEnvelope`` files under agent directories.

    File layout: ``/agents/{agent_id}/tasks/{timestamp}_{task_id}.json``

    Parameters
    ----------
    storage:
        A ``VFSOperations`` implementation (kernel VFS via KernelVFSAdapter,
        local filesystem, or in-memory fake).
    max_cache_size:
        Maximum entries in the task index before LRU eviction.
    """

    def __init__(
        self,
        storage: "VFSOperations",
        max_cache_size: int = _DEFAULT_MAX_CACHE_SIZE,
    ) -> None:
        self._storage = storage
        self._max_cache_size = max_cache_size
        # LRU index: task_id -> _IndexEntry (location on disk)
        self._task_index: OrderedDict[str, _IndexEntry] = OrderedDict()
        # Per-task lock prevents duplicate file creation on concurrent saves
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Public API (TaskStoreProtocol)
    # ------------------------------------------------------------------

    async def save(
        self,
        task: Task,
        *,
        zone_id: str,
        agent_id: str | None = None,
    ) -> None:
        effective_agent_id = agent_id or _UNASSIGNED_AGENT
        _validate_id(effective_agent_id, "agent_id")
        _validate_id(task.id, "task_id")
        _validate_id(zone_id, "zone_id")

        t0 = time.monotonic()
        tasks_dir = tasks_path(effective_agent_id)
        await self._storage.mkdir(tasks_dir, zone_id)

        # Wrap task in MessageEnvelope (§17.6 convergence)
        # Use model_validate with alias keys ("from"/"to") to satisfy mypy;
        # Pydantic's populate_by_name works at runtime but mypy only sees aliases.
        envelope = MessageEnvelope.model_validate(
            {
                "from": "a2a_gateway",
                "to": effective_agent_id,
                "type": MessageType.TASK,
                "correlation_id": task.id,
                "payload": task.model_dump(mode="json"),
            }
        )
        data = envelope.to_bytes()

        # Lock per task_id to prevent duplicate file creation
        lock = self._locks.setdefault(task.id, asyncio.Lock())
        async with lock:
            existing = self._task_index.get(task.id)

            if existing is None:
                existing_filename = await self._scan_for_task(
                    task.id,
                    effective_agent_id,
                    zone_id,
                )
                if existing_filename is not None:
                    existing = _IndexEntry(zone_id, effective_agent_id, existing_filename)

            if existing is not None:
                # Update existing file in place
                path = f"{tasks_path(existing.agent_id)}/{existing.filename}"
                await self._storage.write(path, data, zone_id)
                # Refresh LRU position
                self._task_index.move_to_end(task.id)
            else:
                # New file with timestamp prefix
                ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
                filename = f"{ts}_{task.id}.json"
                path = f"{tasks_dir}/{filename}"
                await self._storage.write(path, data, zone_id)
                self._index_put(
                    task.id,
                    _IndexEntry(zone_id, effective_agent_id, filename),
                )

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.debug(
            "vfs_task_store.save task_id=%s agent_id=%s duration_ms=%.1f",
            task.id,
            effective_agent_id,
            elapsed_ms,
        )

    async def get(self, task_id: str, *, zone_id: str) -> Task | None:
        _validate_id(task_id, "task_id")
        _validate_id(zone_id, "zone_id")

        entry = self._task_index.get(task_id)

        if entry is not None and entry.zone_id == zone_id:
            # Cache hit with matching zone — refresh LRU position
            self._task_index.move_to_end(task_id)
            path = f"{tasks_path(entry.agent_id)}/{entry.filename}"
            try:
                data = await self._storage.read(path, zone_id)
            except FileNotFoundError:
                self._task_index.pop(task_id, None)
                self._locks.pop(task_id, None)
                return None
            return _extract_task(data)

        # Cache miss or zone mismatch — lazy scan all agent directories
        return await self._scan_all_agents_for_task(task_id, zone_id)

    async def delete(self, task_id: str, *, zone_id: str) -> bool:
        _validate_id(task_id, "task_id")
        _validate_id(zone_id, "zone_id")

        entry = self._task_index.get(task_id)

        # Zone isolation: ignore cross-zone cache hit
        if entry is not None and entry.zone_id != zone_id:
            entry = None

        if entry is None:
            # Try to find it via scan
            task = await self._scan_all_agents_for_task(task_id, zone_id)
            if task is None:
                return False
            entry = self._task_index.get(task_id)
            if entry is None:
                return False

        src_path = f"{tasks_path(entry.agent_id)}/{entry.filename}"
        dl_dir = task_dead_letter_path(entry.agent_id)
        await self._storage.mkdir(dl_dir, zone_id)
        dst_path = f"{dl_dir}/{entry.filename}"

        try:
            await self._storage.rename(src_path, dst_path, zone_id)
        except FileNotFoundError:
            self._task_index.pop(task_id, None)
            self._locks.pop(task_id, None)
            return False

        self._task_index.pop(task_id, None)
        self._locks.pop(task_id, None)
        return True

    async def list_tasks(
        self,
        *,
        zone_id: str,
        agent_id: str | None = None,
        state: TaskState | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        if agent_id is not None:
            return await self._list_agent_tasks(
                agent_id=agent_id,
                zone_id=zone_id,
                state=state,
                limit=limit,
                offset=offset,
            )

        # Zone-wide listing: scan all agent directories
        return await self._list_all_agents_tasks(
            zone_id=zone_id,
            state=state,
            limit=limit,
            offset=offset,
        )

    # ------------------------------------------------------------------
    # Internal: per-agent listing
    # ------------------------------------------------------------------

    async def _list_agent_tasks(
        self,
        *,
        agent_id: str,
        zone_id: str,
        state: TaskState | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        tasks_dir = tasks_path(agent_id)
        try:
            entries = await self._storage.list_dir(tasks_dir, zone_id)
        except FileNotFoundError:
            return []

        json_files = sorted(
            (e for e in entries if e.endswith(".json")),
            reverse=True,
        )

        results: list[Task] = []
        skipped = 0
        for filename in json_files:
            path = f"{tasks_dir}/{filename}"
            try:
                data = await self._storage.read(path, zone_id)
            except FileNotFoundError:
                continue

            task = _extract_task(data)
            if task is None:
                continue

            if state is not None and task.status.state != state:
                continue

            if skipped < offset:
                skipped += 1
                continue

            results.append(task)
            self._index_put(task.id, _IndexEntry(zone_id, agent_id, filename))

            if len(results) >= limit:
                break

        return results

    async def _list_all_agents_tasks(
        self,
        *,
        zone_id: str,
        state: TaskState | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        """Scan all agent directories for tasks (zone-wide listing)."""
        try:
            agent_dirs = await self._storage.list_dir(AGENTS_ROOT, zone_id)
        except FileNotFoundError:
            return []

        # Collect (filename, agent_id) tuples across all agents for sorting
        all_entries: list[tuple[str, str]] = []
        for agent_dir_name in agent_dirs:
            tasks_dir = tasks_path(agent_dir_name)
            try:
                entries = await self._storage.list_dir(tasks_dir, zone_id)
            except FileNotFoundError:
                continue
            for entry in entries:
                if entry.endswith(".json"):
                    all_entries.append((entry, agent_dir_name))

        # Sort by filename descending (timestamp prefix = newest first)
        all_entries.sort(key=lambda pair: pair[0], reverse=True)

        results: list[Task] = []
        skipped = 0
        for filename, agent_dir_name in all_entries:
            path = f"{tasks_path(agent_dir_name)}/{filename}"
            try:
                data = await self._storage.read(path, zone_id)
            except FileNotFoundError:
                continue

            task = _extract_task(data)
            if task is None:
                continue

            if state is not None and task.status.state != state:
                continue

            if skipped < offset:
                skipped += 1
                continue

            results.append(task)
            self._index_put(
                task.id,
                _IndexEntry(zone_id, agent_dir_name, filename),
            )

            if len(results) >= limit:
                break

        return results

    # ------------------------------------------------------------------
    # Internal: scanning and indexing
    # ------------------------------------------------------------------

    async def _scan_for_task(
        self,
        task_id: str,
        agent_id: str,
        zone_id: str,
    ) -> str | None:
        """Scan a specific agent's tasks/ directory for a task file."""
        tasks_dir = tasks_path(agent_id)
        suffix = f"_{task_id}.json"
        try:
            entries = await self._storage.list_dir(tasks_dir, zone_id)
        except FileNotFoundError:
            return None

        for entry in entries:
            if entry.endswith(suffix):
                self._index_put(
                    task_id,
                    _IndexEntry(zone_id, agent_id, entry),
                )
                return entry
        return None

    async def _scan_all_agents_for_task(
        self,
        task_id: str,
        zone_id: str,
    ) -> Task | None:
        """Scan all agent directories to find a task by ID (cold start fallback)."""
        logger.debug(
            "vfs_task_store: index miss for task_id=%s, scanning agent dirs",
            task_id,
        )

        try:
            agent_dirs = await self._storage.list_dir(AGENTS_ROOT, zone_id)
        except FileNotFoundError:
            return None

        suffix = f"_{task_id}.json"
        for agent_dir_name in agent_dirs:
            tasks_dir = tasks_path(agent_dir_name)
            try:
                entries = await self._storage.list_dir(tasks_dir, zone_id)
            except FileNotFoundError:
                continue

            for entry in entries:
                if entry.endswith(suffix):
                    path = f"{tasks_dir}/{entry}"
                    try:
                        data = await self._storage.read(path, zone_id)
                    except FileNotFoundError:
                        continue

                    task = _extract_task(data)
                    if task is not None:
                        self._index_put(
                            task_id,
                            _IndexEntry(zone_id, agent_dir_name, entry),
                        )
                        return task

        return None

    def _index_put(self, task_id: str, entry: _IndexEntry) -> None:
        """Add or update an index entry with LRU eviction."""
        if task_id in self._task_index:
            self._task_index.move_to_end(task_id)
        self._task_index[task_id] = entry

        # Evict oldest entries if over capacity
        while len(self._task_index) > self._max_cache_size:
            evicted_id, _ = self._task_index.popitem(last=False)
            self._locks.pop(evicted_id, None)


# ======================================================================
# Helpers
# ======================================================================


def _validate_id(value: str, name: str) -> None:
    """Validate an ID string to prevent path traversal."""
    if not value or not _SAFE_ID_RE.match(value):
        raise ValueError(f"Invalid {name}: {value!r}")


def _extract_task(data: bytes) -> Task | None:
    """Extract a Task from MessageEnvelope bytes.

    Returns *None* if the data is not a valid task envelope.
    """
    try:
        envelope = MessageEnvelope.from_bytes(data)
        return Task.model_validate(envelope.payload)
    except Exception:
        logger.warning("Failed to extract task from envelope", exc_info=True)
        return None
