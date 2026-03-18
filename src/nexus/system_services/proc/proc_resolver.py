"""ProcResolver — procfs virtual filesystem for ProcessTable.

Implements VFSPathResolver ``try_*`` protocol (#1665) to provide
``/{zone}/proc/{pid}/status`` as virtual files generated from
ProcessTable's in-memory state at read time.  Like Linux ``/proc``,
nothing is stored on disk.

    system_services/proc/proc_resolver.py = fs/proc/ (procfs)
    core/process_table.py                 = kernel/fork.c (task_struct table)

Registration: factory/orchestrator.py registers ProcResolver via
coordinator.enlist() at boot, after ProcessTable creation.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from nexus.contracts.protocols.service_hooks import HookSpec

if TYPE_CHECKING:
    from nexus.core.process_table import ProcessTable

logger = logging.getLogger(__name__)

# Match /{zone}/proc/{pid}/status
_PROC_STATUS_RE = re.compile(r"^/([^/]+)/proc/([^/]+)/status$")


class ProcResolver:
    """VFSPathResolver for /{zone}/proc/{pid}/status (procfs model).

    Single-call ``try_*`` pattern (#1665): each method returns ``None``
    when the path is not a proc path, or the result when it is.
    Write and delete raise PermissionError (read-only, like /proc).
    """

    TRIE_PATTERN = "/{}/proc/{}/status"

    def __init__(self, process_table: ProcessTable) -> None:
        self._process_table = process_table

    # -- HotSwappable protocol (registered via coordinator.enlist) --

    def hook_spec(self) -> HookSpec:
        return HookSpec(resolvers=(self,))

    async def drain(self) -> None:
        pass

    async def activate(self) -> None:
        pass

    def _match_pid(self, path: str) -> str | None:
        """Extract PID from path if it matches and process exists."""
        m = _PROC_STATUS_RE.match(path)
        if m is None:
            return None
        pid = m.group(2)
        if self._process_table.get(pid) is None:
            return None
        return pid

    def try_read(
        self,
        path: str,
        *,
        return_metadata: bool = False,
        context: Any = None,
    ) -> bytes | dict | None:
        """Generate process status JSON from in-memory state, or None."""
        _ = context
        pid = self._match_pid(path)
        if pid is None:
            return None

        desc = self._process_table.get(pid)
        if desc is None:
            return None

        body = json.dumps(desc.to_dict(), ensure_ascii=False, indent=2).encode()

        if return_metadata:
            return {
                "content": body,
                "size": len(body),
                "entry_type": 0,  # DT_REG
            }
        return body

    def try_write(self, path: str, _content: bytes) -> dict[str, Any] | None:
        """Reject writes on proc paths (read-only, like Linux /proc)."""
        if self._match_pid(path) is not None:
            raise PermissionError(f"{path}: proc filesystem is read-only")
        return None

    def try_delete(self, path: str, *, context: Any = None) -> dict[str, Any] | None:
        """Reject deletes on proc paths (read-only, like Linux /proc)."""
        _ = context
        if self._match_pid(path) is not None:
            raise PermissionError(f"{path}: proc filesystem is read-only")
        return None
