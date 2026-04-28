"""AgentStatusResolver — procfs virtual filesystem for AgentRegistry.

Implements the VFSPathResolver ``try_*`` protocol to provide
``/{zone}/proc/{pid}/status`` as virtual files generated from the
AgentRegistry in-memory state at read time. Like Linux ``/proc``,
nothing is stored on disk.

    services/agents/agent_status_resolver.py = fs/proc/ (procfs)
    services/agents/agent_registry.py        = kernel/fork.c (task_struct table)

Registration: factory/orchestrator.py registers AgentStatusResolver via
coordinator.enlist() at boot, after AgentRegistry creation.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from nexus.contracts.protocols.service_hooks import HookSpec

if TYPE_CHECKING:
    from nexus.services.agents.agent_registry import AgentRegistry

logger = logging.getLogger(__name__)

# Match /{zone}/proc/{pid}/status
_PROC_STATUS_RE = re.compile(r"^/([^/]+)/proc/([^/]+)/status$")


class AgentStatusResolver:
    """VFSPathResolver for /{zone}/proc/{pid}/status (procfs model).

    Single-call ``try_*`` pattern (#1665): each method returns ``None``
    when the path is not a proc path, or the result when it is.
    Write and delete raise PermissionError (read-only, like /proc).
    """

    TRIE_PATTERN = "/{}/proc/{}/status"

    def __init__(self, agent_registry: AgentRegistry) -> None:
        self._agent_registry = agent_registry

    # -- Hook spec (duck-typed) (registered via coordinator.enlist) --

    def hook_spec(self) -> HookSpec:
        return HookSpec(resolvers=(self,))

    def _match_pid(self, path: str) -> str | None:
        """Extract PID from path if it matches and process exists."""
        m = _PROC_STATUS_RE.match(path)
        if m is None:
            return None
        pid = m.group(2)
        if self._agent_registry.get(pid) is None:
            return None
        return pid

    def try_read(
        self,
        path: str,
        *,
        context: Any = None,
    ) -> bytes | None:
        """Generate process status JSON from in-memory state, or None."""
        _ = context
        pid = self._match_pid(path)
        if pid is None:
            return None

        desc = self._agent_registry.get(pid)
        if desc is None:
            return None

        return json.dumps(desc.to_dict(), ensure_ascii=False, indent=2).encode()

    def try_write(
        self, path: str, _content: bytes, *, context: Any = None
    ) -> dict[str, Any] | None:
        """Reject writes on proc paths (read-only, like Linux /proc)."""
        _ = context
        if self._match_pid(path) is not None:
            raise PermissionError(f"{path}: proc filesystem is read-only")
        return None

    def try_delete(self, path: str, *, context: Any = None) -> dict[str, Any] | None:
        """Reject deletes on proc paths (read-only, like Linux /proc)."""
        _ = context
        if self._match_pid(path) is not None:
            raise PermissionError(f"{path}: proc filesystem is read-only")
        return None
