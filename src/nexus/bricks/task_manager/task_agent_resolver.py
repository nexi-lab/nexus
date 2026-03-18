"""VFS read resolver: /.tasks/tasks/{task_id}/agent/status → live ProcessDescriptor.

Intercepts reads to the virtual path and returns the ProcessDescriptor
for the task's worker agent, assembled live from ProcessTable.
No data is stored on disk — like Linux /proc, it is generated on demand.

Virtual path: /.tasks/tasks/{task_id}/agent/status
Source of truth: worker_pid field in task JSON (set by TaskDispatchPipeConsumer)
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.contracts.protocols.service_hooks import HookSpec

_AGENT_STATUS_RE = re.compile(r"^/\.tasks/tasks/([^/]+)/agent/status$")


class TaskAgentResolver:
    """VFSPathResolver for /.tasks/tasks/{task_id}/agent/status.

    Follows the ``try_*`` protocol (#1665): each method returns ``None``
    when the path is not claimed, or the result/raises when it is.
    Write and delete raise PermissionError (read-only virtual path).
    """

    TRIE_PATTERN = "/.tasks/tasks/{}/agent/status"

    def __init__(self, process_table: Any) -> None:
        self._process_table = process_table
        self._worker_pids: dict[str, int] = {}  # task_id → worker_pid (sync cache)

    def hook_spec(self) -> "HookSpec":
        from nexus.contracts.protocols.service_hooks import HookSpec

        return HookSpec(resolvers=(self,))

    async def drain(self) -> None:
        pass

    async def activate(self) -> None:
        pass

    def notify_worker_assigned(self, task_id: str, worker_pid: int) -> None:
        """Called by dispatch consumer when a worker is assigned to a task."""
        self._worker_pids[task_id] = worker_pid

    def _match_task_id(self, path: str) -> str | None:
        m = _AGENT_STATUS_RE.match(path)
        return m.group(1) if m is not None else None

    def try_read(
        self,
        path: str,
        *,
        return_metadata: bool = False,
        context: Any = None,
    ) -> bytes | dict | None:
        """Return agent status JSON for the task's worker, or None."""
        _ = context
        task_id = self._match_task_id(path)
        if task_id is None:
            return None  # not our path — pass through

        worker_pid = self._worker_pids.get(task_id)
        if not worker_pid:
            payload: dict[str, Any] = {"status": "no_worker", "task_id": task_id}
        else:
            proc = self._process_table.get(worker_pid) if self._process_table else None
            if proc is None:
                payload = {"status": "exited", "task_id": task_id, "worker_pid": worker_pid}
            else:
                payload = proc.to_dict()
                payload["task_id"] = task_id

        body = json.dumps(payload, ensure_ascii=False).encode()

        if return_metadata:
            return {
                "content": body,
                "size": len(body),
                "entry_type": 0,  # DT_REG
            }
        return body

    def try_write(self, path: str, _content: bytes) -> dict[str, Any] | None:
        """Reject writes on agent status paths (read-only virtual path)."""
        if self._match_task_id(path) is not None:
            raise PermissionError(f"{path}: task agent status is read-only")
        return None

    def try_delete(self, path: str, *, context: Any = None) -> dict[str, Any] | None:
        """Reject deletes on agent status paths (read-only virtual path)."""
        _ = context
        if self._match_task_id(path) is not None:
            raise PermissionError(f"{path}: task agent status is read-only")
        return None
