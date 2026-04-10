"""TaskManagerService — NexusFS-backed task and mission management.

All data is stored as JSON files under ``/.tasks/`` in NexusFS.  This
dogfoods the platform and gets file events, permissions, and CAS
deduplication for free — no migrations, no new DB tables.
"""

from __future__ import annotations

import contextlib
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from nexus.contracts.vfs_paths import task as task_paths

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

# Maps (current_status) -> set of valid next statuses
_TASK_TRANSITIONS: dict[str, set[str]] = {
    "created": {"running", "failed", "cancelled"},
    "running": {"in_review", "completed", "failed", "cancelled"},
    "in_review": {"running", "completed", "failed", "cancelled"},
    "completed": {"cancelled"},
    "failed": {"cancelled"},
    "cancelled": set(),
}

_VALID_TASK_STATUSES = frozenset(_TASK_TRANSITIONS.keys())

_VALID_MISSION_STATUSES = frozenset({"running", "partial_complete", "completed", "cancelled"})

_VALID_ARTIFACT_TYPES = frozenset(
    {
        "document",
        "code",
        "folder",
        "pr",
        "image",
        "data",
        "spreadsheet",
        "presentation",
        "other",
    }
)


class ValidationError(Exception):
    """Raised when a state-machine transition or input is invalid."""


class NotFoundError(Exception):
    """Raised when the requested entity does not exist."""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class TaskManagerService:
    """Core brick for task/mission management, backed by NexusFS JSON files."""

    def __init__(self, nexus_fs: Any) -> None:
        self._fs = nexus_fs
        self._dirs_ready = False

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _mission_path(mission_id: str) -> str:
        return task_paths.mission(mission_id)

    @staticmethod
    def _task_path(task_id: str) -> str:
        return task_paths.item(task_id)

    @staticmethod
    def _comment_dir(task_id: str) -> str:
        return f"{task_paths.ROOT}/comments/{task_id}"

    @staticmethod
    def _comment_path(task_id: str, comment_id: str) -> str:
        return task_paths.comment(task_id, comment_id)

    @staticmethod
    def _artifact_path(artifact_id: str) -> str:
        return task_paths.artifact(artifact_id)

    # ------------------------------------------------------------------
    # I/O helpers
    # ------------------------------------------------------------------

    async def _read_json(self, path: str) -> dict[str, Any]:
        raw = self._fs.sys_read(path)
        result: dict[str, Any] = json.loads(raw)
        return result

    async def _write_json(self, path: str, data: dict[str, Any]) -> None:
        await self._ensure_dirs()
        self._fs.write(path, json.dumps(data, default=str))

    @staticmethod
    def _audit_dir(task_id: str) -> str:
        return f"{task_paths.ROOT}/audit/{task_id}"

    @staticmethod
    def _audit_path(task_id: str, entry_id: str) -> str:
        return task_paths.audit_entry(task_id, entry_id)

    async def _ensure_dirs(self) -> None:
        """Create required VFS directories on first use."""
        if self._dirs_ready:
            return
        for d in (
            "/.tasks",
            "/.tasks/missions",
            "/.tasks/tasks",
            "/.tasks/artifacts",
            "/.tasks/comments",
            "/.tasks/audit",
        ):
            self._fs.mkdir(d, parents=True, exist_ok=True)
        self._dirs_ready = True

    def _now(self) -> str:
        return datetime.now(UTC).isoformat()

    # ------------------------------------------------------------------
    # Copilot methods
    # ------------------------------------------------------------------

    async def create_mission(
        self,
        title: str,
        context_summary: str | None = None,
    ) -> dict[str, Any]:
        """Create a new mission."""
        mission_id = uuid.uuid4().hex
        now = self._now()
        doc: dict[str, Any] = {
            "id": mission_id,
            "title": title,
            "status": "running",
            "context_summary": context_summary,
            "conclusion": None,
            "archived": False,
            "created_at": now,
            "updated_at": now,
        }
        await self._write_json(self._mission_path(mission_id), doc)
        return doc

    async def update_mission(self, mission_id: str, **fields: Any) -> dict[str, Any]:
        """Update mission fields (status, conclusion, archived)."""
        path = self._mission_path(mission_id)
        try:
            doc = await self._read_json(path)
        except Exception as exc:
            raise NotFoundError(f"Mission {mission_id} not found") from exc

        allowed = {"status", "conclusion", "archived", "title", "context_summary"}
        for key, value in fields.items():
            if key not in allowed:
                raise ValidationError(f"Cannot update field '{key}' on mission")
            if key == "status" and value not in _VALID_MISSION_STATUSES:
                raise ValidationError(
                    f"Invalid mission status '{value}'. "
                    f"Must be one of: {sorted(_VALID_MISSION_STATUSES)}"
                )
            doc[key] = value

        doc["updated_at"] = self._now()
        await self._write_json(path, doc)
        return doc

    async def create_task(
        self,
        mission_id: str,
        instruction: str,
        *,
        worker_type: str | None = None,
        input_refs: list[str] | None = None,
        blocked_by: list[str] | None = None,
        deadline: str | None = None,
        estimated_duration: int | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        """Create a new task within a mission."""
        # Verify mission exists; reopen if completed
        try:
            mission = await self._read_json(self._mission_path(mission_id))
        except Exception as exc:
            raise NotFoundError(f"Mission {mission_id} not found") from exc

        if mission.get("status") in ("completed", "partial_complete"):
            mission["status"] = "running"
            mission["updated_at"] = self._now()
            await self._write_json(self._mission_path(mission_id), mission)
            logger.info("[TASK-MGR] Mission %s reopened (new task added)", mission_id)

        task_id = uuid.uuid4().hex
        now = self._now()
        doc: dict[str, Any] = {
            "id": task_id,
            "mission_id": mission_id,
            "instruction": instruction,
            "status": "created",
            "worker_type": worker_type,
            "input_refs": input_refs or [],
            "output_refs": [],
            "blocked_by": blocked_by or [],
            "label": label,
            "deadline": deadline,
            "estimated_duration": estimated_duration,
            "created_at": now,
            "started_at": None,
            "completed_at": None,
            "worker_pid": None,  # set when AcpService spawns the worker agent
            "agent_name": None,  # set from AcpResult.agent_id after spawn
        }
        await self._write_json(self._task_path(task_id), doc)
        return doc

    async def update_task(self, task_id: str, **fields: Any) -> dict[str, Any]:
        """Update task fields — enforces state machine for status changes."""
        allowed = {"status", "output_refs", "worker_pid", "agent_name"}
        for key in fields:
            if key not in allowed:
                raise ValidationError(f"Cannot update field '{key}' on task")

        path = self._task_path(task_id)
        try:
            doc = await self._read_json(path)
        except Exception as exc:
            raise NotFoundError(f"Task {task_id} not found") from exc

        if "status" in fields:
            new_status = fields["status"]
            current = doc["status"]
            valid_next = _TASK_TRANSITIONS.get(current, set())
            if new_status not in valid_next:
                raise ValidationError(
                    f"Invalid transition: {current} → {new_status}. Allowed: {sorted(valid_next)}"
                )
            doc["status"] = new_status

            # Auto-set timestamps
            if new_status == "running" and doc["started_at"] is None:
                doc["started_at"] = self._now()
            if new_status in ("completed", "failed", "cancelled"):
                doc["completed_at"] = self._now()

        if "output_refs" in fields:
            doc["output_refs"] = fields["output_refs"]

        for field in ("worker_pid", "agent_name"):
            if field in fields:
                doc[field] = fields[field]

        await self._write_json(path, doc)

        # Auto-complete mission when all its tasks are terminal
        if "status" in fields and fields["status"] in ("completed", "failed", "cancelled"):
            await self._maybe_complete_mission(doc.get("mission_id"))

        return doc

    async def get_task(self, task_id: str) -> dict[str, Any]:
        """Get a single task by ID."""
        try:
            return await self._read_json(self._task_path(task_id))
        except Exception as exc:
            raise NotFoundError(f"Task {task_id} not found") from exc

    async def create_comment(
        self,
        task_id: str,
        author: str,
        content: str,
        artifact_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a comment on a task."""
        if not author or not isinstance(author, str):
            raise ValidationError("author must be a non-empty string")

        # Verify task exists
        await self.get_task(task_id)

        # Ensure per-task comment directory
        self._fs.mkdir(self._comment_dir(task_id), parents=True, exist_ok=True)

        comment_id = uuid.uuid4().hex
        doc: dict[str, Any] = {
            "id": comment_id,
            "task_id": task_id,
            "author": author,
            "content": content,
            "artifact_refs": artifact_refs or [],
            "created_at": self._now(),
        }
        await self._write_json(self._comment_path(task_id, comment_id), doc)
        return doc

    async def get_comments(self, task_id: str) -> list[dict[str, Any]]:
        """Get all comments for a task, ordered by created_at."""
        comment_dir = self._comment_dir(task_id)
        if not self._fs.is_directory(comment_dir):
            return []

        paths = self._fs.sys_readdir(comment_dir, recursive=False)
        comments = []
        for p in paths:
            if p.endswith(".json"):
                try:
                    comments.append(await self._read_json(p))
                except Exception:
                    logger.warning("Failed to read comment at %s", p)
        comments.sort(key=lambda c: c.get("created_at", ""))
        return comments

    async def create_artifact(
        self,
        type: str,
        uri: str,
        title: str,
        *,
        mime_type: str | None = None,
        size_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Create an artifact reference."""
        if type not in _VALID_ARTIFACT_TYPES:
            raise ValidationError(
                f"Invalid artifact type '{type}'. Must be one of: {sorted(_VALID_ARTIFACT_TYPES)}"
            )

        artifact_id = uuid.uuid4().hex
        doc: dict[str, Any] = {
            "id": artifact_id,
            "type": type,
            "uri": uri,
            "title": title,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "created_at": self._now(),
        }
        await self._write_json(self._artifact_path(artifact_id), doc)
        return doc

    # ------------------------------------------------------------------
    # Dispatcher methods
    # ------------------------------------------------------------------

    async def list_dispatchable_tasks(self, worker_type: str | None = None) -> list[dict[str, Any]]:
        """List tasks with status=created whose blocked_by are all completed."""
        all_tasks = await self._list_all_tasks()

        # Build a lookup of task statuses for blocked_by resolution
        status_by_id = {t["id"]: t["status"] for t in all_tasks}

        result = []
        for t in all_tasks:
            if t["status"] != "created":
                continue
            if worker_type and t.get("worker_type") != worker_type:
                continue
            # Check all blockers are completed
            blocked_by = t.get("blocked_by", [])
            if all(status_by_id.get(b) == "completed" for b in blocked_by):
                result.append(t)
        return result

    async def start_task(self, task_id: str) -> dict[str, Any]:
        """Dispatcher: created → running."""
        return await self.update_task(task_id, status="running")

    async def complete_task(
        self, task_id: str, output_refs: list[str] | None = None
    ) -> dict[str, Any]:
        """Worker: running → completed."""
        fields: dict[str, Any] = {"status": "completed"}
        if output_refs is not None:
            fields["output_refs"] = output_refs
        return await self.update_task(task_id, **fields)

    async def fail_task(self, task_id: str) -> dict[str, Any]:
        """Worker: running → failed."""
        return await self.update_task(task_id, status="failed")

    # ------------------------------------------------------------------
    # User methods (read-only)
    # ------------------------------------------------------------------

    async def list_missions(
        self,
        *,
        archived: bool = False,
        status: str | None = None,
        page: int = 1,
        limit: int = 20,
    ) -> dict[str, Any]:
        """List missions with optional filters and pagination."""
        missions_dir = "/.tasks/missions"
        if not self._fs.is_directory(missions_dir):
            return {"items": [], "total": 0, "page": page, "limit": limit}

        paths = self._fs.sys_readdir(missions_dir, recursive=False)
        missions = []
        for p in paths:
            if p.endswith(".json"):
                try:
                    m = await self._read_json(p)
                    if m.get("archived", False) != archived:
                        continue
                    if status and m.get("status") != status:
                        continue
                    missions.append(m)
                except Exception:
                    logger.warning("Failed to read mission at %s", p)

        missions.sort(key=lambda m: m.get("created_at", ""), reverse=True)
        total = len(missions)
        start = (page - 1) * limit
        items = missions[start : start + limit]
        return {"items": items, "total": total, "page": page, "limit": limit}

    async def get_mission(self, mission_id: str) -> dict[str, Any]:
        """Get mission detail with its task list."""
        try:
            mission = await self._read_json(self._mission_path(mission_id))
        except Exception as exc:
            raise NotFoundError(f"Mission {mission_id} not found") from exc

        # Collect tasks for this mission
        all_tasks = await self._list_all_tasks()
        tasks = [t for t in all_tasks if t.get("mission_id") == mission_id]
        tasks.sort(key=lambda t: t.get("created_at", ""))
        mission["tasks"] = tasks
        return mission

    async def get_task_detail(self, task_id: str) -> dict[str, Any]:
        """Get task detail with comments, artifacts, and history."""
        task = await self.get_task(task_id)
        task["comments"] = await self.get_comments(task_id)
        task["history"] = await self.get_task_history(task_id)

        # Resolve artifact references
        all_refs = set(task.get("input_refs", []) + task.get("output_refs", []))
        for comment in task["comments"]:
            all_refs.update(comment.get("artifact_refs", []))

        artifacts = []
        for ref_id in all_refs:
            with contextlib.suppress(Exception):
                artifacts.append(await self._read_json(self._artifact_path(ref_id)))
        task["artifacts"] = artifacts
        return task

    # ------------------------------------------------------------------
    # Audit trail
    # ------------------------------------------------------------------

    async def create_audit_entry(
        self,
        task_id: str,
        action: str,
        *,
        actor: str | None = None,
        detail: str | None = None,
    ) -> dict[str, Any]:
        """Create an audit trail entry for a task."""
        # Verify task exists
        await self.get_task(task_id)

        # Ensure per-task audit directory
        self._fs.mkdir(self._audit_dir(task_id), parents=True, exist_ok=True)

        entry_id = uuid.uuid4().hex
        doc: dict[str, Any] = {
            "id": entry_id,
            "task_id": task_id,
            "action": action,
            "actor": actor,
            "detail": detail,
            "created_at": self._now(),
        }
        await self._write_json(self._audit_path(task_id, entry_id), doc)
        return doc

    async def get_audit_trail(self, task_id: str) -> list[dict[str, Any]]:
        """Get all audit entries for a task, ordered by created_at."""
        audit_dir = self._audit_dir(task_id)
        if not self._fs.is_directory(audit_dir):
            return []

        paths = self._fs.sys_readdir(audit_dir, recursive=False)
        entries = []
        for p in paths:
            if p.endswith(".json"):
                try:
                    entries.append(await self._read_json(p))
                except Exception:
                    logger.warning("Failed to read audit entry at %s", p)
        entries.sort(key=lambda e: e.get("created_at", ""))
        return entries

    async def get_task_history(self, task_id: str) -> list[dict[str, Any]]:
        """Get unified timeline merging audit entries and comments."""
        # Verify task exists
        await self.get_task(task_id)

        history: list[dict[str, Any]] = []

        for entry in await self.get_audit_trail(task_id):
            history.append({"type": "audit", **entry})

        for comment in await self.get_comments(task_id):
            history.append({"type": "comment", **comment})

        history.sort(key=lambda h: h.get("created_at", ""))
        return history

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    _TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

    async def _maybe_complete_mission(self, mission_id: str | None) -> None:
        """Auto-complete the mission if all its tasks have reached a terminal status."""
        if not mission_id:
            return
        try:
            mission = await self._read_json(self._mission_path(mission_id))
        except Exception:
            return
        if mission.get("status") != "running":
            return

        tasks = [t for t in await self._list_all_tasks() if t.get("mission_id") == mission_id]
        if not tasks:
            return
        if all(t.get("status") in self._TERMINAL_STATUSES for t in tasks):
            mission["status"] = "completed"
            mission["updated_at"] = self._now()
            await self._write_json(self._mission_path(mission_id), mission)
            logger.info("[TASK-MGR] Mission %s auto-completed (all tasks terminal)", mission_id)

    async def _list_all_tasks(self) -> list[dict[str, Any]]:
        """Read all task JSON files from NexusFS."""
        tasks_dir = "/.tasks/tasks"
        if not self._fs.is_directory(tasks_dir):
            return []

        paths = self._fs.sys_readdir(tasks_dir, recursive=False)
        tasks = []
        for p in paths:
            if p.endswith(".json"):
                try:
                    tasks.append(await self._read_json(p))
                except Exception:
                    logger.warning("Failed to read task at %s", p)
        return tasks
