"""VFS-backed task store — tasks as JSON files.

File layout::

    {base_path}/{zone_id}/{timestamp}_{task_id}.json

Timestamp prefix enables efficient sorting by creation time without
reading file contents.  The same pattern is used by the IPC
``TTLSweeper`` (Plan P1: filename timestamp skip).

Serialization uses Pydantic's built-in ``model_dump_json()`` /
``model_validate_json()`` for zero-copy roundtrips.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.a2a.models import Task, TaskState

if TYPE_CHECKING:
    from nexus.ipc.storage.protocol import IPCStorageDriver

logger = logging.getLogger(__name__)

_ZONE_ID_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")


class VFSTaskStore:
    """Stores A2A tasks as flat JSON files on a VFS backend.

    Parameters
    ----------
    storage:
        An ``IPCStorageDriver`` implementation (VFS-backed, PostgreSQL,
        or in-memory fake).
    base_path:
        Root directory for task files.  Defaults to ``/a2a/tasks``.
    """

    def __init__(
        self,
        storage: IPCStorageDriver,
        base_path: str = "/a2a/tasks",
    ) -> None:
        self._storage = storage
        self._base_path = base_path.rstrip("/")
        # Cache: (zone_id, task_id) -> filename for fast lookups after save
        self._filename_cache: dict[tuple[str, str], str] = {}
        # Per-key lock prevents duplicate file creation on concurrent saves
        self._locks: defaultdict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)

    async def save(
        self,
        task: Task,
        *,
        zone_id: str,
        agent_id: str | None = None,
    ) -> None:
        _validate_zone_id(zone_id)
        t0 = time.monotonic()
        zone_dir = f"{self._base_path}/{zone_id}"
        await self._storage.mkdir(zone_dir, zone_id)

        # Build envelope with agent_id (not part of Task model)
        envelope: dict[str, Any] = {
            "agent_id": agent_id,
            "task": task.model_dump(mode="json"),
        }
        data = _serialize_envelope(envelope)

        # Lock per (zone_id, task_id) to prevent duplicate file creation
        cache_key = (zone_id, task.id)
        async with self._locks[cache_key]:
            existing_filename = self._filename_cache.get(cache_key)

            if existing_filename is None:
                existing_filename = await self._find_filename(task.id, zone_id)

            if existing_filename is not None:
                # Update existing file in place
                path = f"{zone_dir}/{existing_filename}"
                await self._storage.write(path, data, zone_id)
            else:
                # New file with timestamp prefix
                ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
                filename = f"{ts}_{task.id}.json"
                path = f"{zone_dir}/{filename}"
                await self._storage.write(path, data, zone_id)
                self._filename_cache[cache_key] = filename

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.debug("vfs_task_store.save task_id=%s duration_ms=%.1f", task.id, elapsed_ms)

    async def get(self, task_id: str, *, zone_id: str) -> Task | None:
        filename = self._filename_cache.get((zone_id, task_id))
        if filename is None:
            filename = await self._find_filename(task_id, zone_id)
        if filename is None:
            return None

        zone_dir = f"{self._base_path}/{zone_id}"
        path = f"{zone_dir}/{filename}"
        try:
            data = await self._storage.read(path, zone_id)
        except FileNotFoundError:
            self._filename_cache.pop((zone_id, task_id), None)
            return None

        envelope = _deserialize_envelope(data)
        return Task.model_validate(envelope["task"])

    async def delete(self, task_id: str, *, zone_id: str) -> bool:
        filename = self._filename_cache.get((zone_id, task_id))
        if filename is None:
            filename = await self._find_filename(task_id, zone_id)
        if filename is None:
            return False

        zone_dir = f"{self._base_path}/{zone_id}"
        path = f"{zone_dir}/{filename}"

        # Overwrite with empty to "delete" (some VFS backends don't support real delete)
        # For IPCStorageDriver we can just write an empty marker then remove from cache.
        # Actually, we need a real delete. Use rename to a dead_letter path.
        dead_letter_dir = f"{self._base_path}/_dead_letter/{zone_id}"
        await self._storage.mkdir(dead_letter_dir, zone_id)
        dead_path = f"{dead_letter_dir}/{filename}"
        try:
            await self._storage.rename(path, dead_path, zone_id)
        except FileNotFoundError:
            self._filename_cache.pop((zone_id, task_id), None)
            return False

        self._filename_cache.pop((zone_id, task_id), None)
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
        zone_dir = f"{self._base_path}/{zone_id}"
        try:
            entries = await self._storage.list_dir(zone_dir, zone_id)
        except FileNotFoundError:
            return []

        # Filter to .json files only (skip directories like _dead_letter)
        json_files = [e for e in entries if e.endswith(".json")]

        # Sort by filename descending (timestamp prefix = newest first)
        json_files.sort(reverse=True)

        # Read and filter
        results: list[Task] = []
        skipped = 0
        for filename in json_files:
            path = f"{zone_dir}/{filename}"
            try:
                data = await self._storage.read(path, zone_id)
            except FileNotFoundError:
                continue

            envelope = _deserialize_envelope(data)
            task = Task.model_validate(envelope["task"])

            # Apply filters
            if state is not None and task.status.state != state:
                continue
            if agent_id is not None and envelope.get("agent_id") != agent_id:
                continue

            # Apply offset
            if skipped < offset:
                skipped += 1
                continue

            results.append(task)

            # Cache the filename for future lookups
            self._filename_cache[(zone_id, task.id)] = filename

            if len(results) >= limit:
                break

        return results

    async def _find_filename(self, task_id: str, zone_id: str) -> str | None:
        """Scan directory to find the file for a given task_id."""
        zone_dir = f"{self._base_path}/{zone_id}"
        suffix = f"_{task_id}.json"
        try:
            entries = await self._storage.list_dir(zone_dir, zone_id)
        except FileNotFoundError:
            return None

        for entry in entries:
            if entry.endswith(suffix):
                self._filename_cache[(zone_id, task_id)] = entry
                return entry
        return None


# ======================================================================
# Helpers
# ======================================================================


def _validate_zone_id(zone_id: str) -> None:
    """Validate zone_id to prevent path traversal."""
    if not zone_id or not _ZONE_ID_RE.match(zone_id):
        raise ValueError(f"Invalid zone_id: {zone_id!r}")


def _serialize_envelope(envelope: dict[str, Any]) -> bytes:
    """Serialize task envelope to JSON bytes."""
    return json.dumps(envelope, default=str, indent=2).encode("utf-8")


def _deserialize_envelope(data: bytes) -> dict[str, Any]:
    """Deserialize task envelope from JSON bytes."""
    result: dict[str, Any] = json.loads(data)
    return result
