"""AgentLogResolver — VFSPathResolver for /.activity/ virtual overlay.

Surfaces the in-memory `MemoryBackend` from `services/activity` as a
read-only mount at `/.activity/{utc_date}/{agent_id}.jsonl`. Each agent
sees only their own file.

Permission model: the resolver inspects the OperationContext for the
caller's `agent_id`. A non-admin caller may only read the file whose
filename matches their own `agent_id`. Admins may read any agent's file.
Writes and deletes always raise PermissionError.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.contracts.protocols.service_hooks import HookSpec


_MOUNT_PREFIX = "/.activity/"
_FILE_RE = re.compile(r"^/\.activity/(\d{4}-\d{2}-\d{2})/([A-Za-z0-9_.\-:]{1,128})\.jsonl$")
_DATE_DIR_RE = re.compile(r"^/\.activity/(\d{4}-\d{2}-\d{2})/?$")


def _caller_identity(context: Any) -> tuple[str | None, bool]:
    """Return (agent_id, is_admin) from the OperationContext.

    Falls back to (None, False) when context is missing fields — the
    resolver then denies access (no anonymous reads).
    """
    if context is None:
        return None, False
    if isinstance(context, dict):
        return context.get("agent_id"), bool(context.get("is_admin", False))
    return getattr(context, "agent_id", None), bool(getattr(context, "is_admin", False))


class AgentLogResolver:
    """Read-only VFS overlay for `/.activity/{date}/{agent_id}.jsonl`.

    The store reference is fetched lazily via ``get_store`` so the
    resolver can be registered at orchestrator boot time — before
    ``setup_activity`` constructs the MemoryBackend.
    """

    __slots__ = ("_get_store",)

    def __init__(self, get_store: Callable[[], Any]) -> None:
        self._get_store = get_store

    def hook_spec(self) -> "HookSpec":
        from nexus.contracts.protocols.service_hooks import HookSpec

        return HookSpec(resolvers=(self,))

    def try_read(self, path: str, *, context: Any = None) -> bytes | None:
        m = _FILE_RE.match(path)
        if m is None:
            return None
        store = self._get_store()
        if store is None:
            return None
        path_agent = m.group(2)
        caller_agent, is_admin = _caller_identity(context)
        if not is_admin and caller_agent != path_agent:
            return None
        data: bytes = store.read_path(path)
        return data

    def try_list(
        self, path: str, *, context: Any = None, recursive: bool = False
    ) -> list[tuple[str, int]] | None:
        norm = path.rstrip("/")
        if not norm.startswith(_MOUNT_PREFIX.rstrip("/")):
            return None
        store = self._get_store()
        if store is None:
            return None
        if norm == _MOUNT_PREFIX.rstrip("/"):
            dates = store.iter_dates()
            entries: list[tuple[str, int]] = [(f"{_MOUNT_PREFIX}{d}", 1) for d in dates]
            if recursive:
                for d in dates:
                    entries.extend(self._list_date_for_caller(d, context, store))
            return entries

        m = _DATE_DIR_RE.match(path)
        if m is not None:
            date = m.group(1)
            return self._list_date_for_caller(date, context, store)
        return None

    def _list_date_for_caller(self, date: str, context: Any, store: "Any") -> list[tuple[str, int]]:
        caller_agent, is_admin = _caller_identity(context)
        files = store.list_dir(f"{_MOUNT_PREFIX}{date}/")
        out: list[tuple[str, int]] = []
        for fname in files:
            agent = fname[: -len(".jsonl")] if fname.endswith(".jsonl") else fname
            if is_admin or agent == caller_agent:
                out.append((f"{_MOUNT_PREFIX}{date}/{fname}", 0))
        return out

    def try_stat(self, path: str, *, context: Any = None) -> dict[str, Any] | None:
        if not path.startswith(_MOUNT_PREFIX):
            return None
        store = self._get_store()
        if store is None:
            return None
        m = _FILE_RE.match(path)
        if m is not None:
            path_agent = m.group(2)
            caller_agent, is_admin = _caller_identity(context)
            if not is_admin and caller_agent != path_agent:
                return None
            data = store.read_path(path)
            return {"path": path, "size": len(data), "etag": "", "entry_type": 0}

        norm = path.rstrip("/")
        if norm == _MOUNT_PREFIX.rstrip("/"):
            return {"path": path, "size": 0, "etag": "", "entry_type": 1}
        if _DATE_DIR_RE.match(path):
            return {"path": path, "size": 0, "etag": "", "entry_type": 1}
        return None

    def try_write(self, path: str, content: bytes, *, context: Any = None) -> dict[str, Any] | None:
        _ = content, context  # read-only — content/context ignored
        if path.startswith(_MOUNT_PREFIX):
            raise PermissionError(f"{path}: /.activity/ is read-only")
        return None

    def try_delete(self, path: str, *, context: Any = None) -> dict[str, Any] | None:
        _ = context
        if path.startswith(_MOUNT_PREFIX):
            raise PermissionError(f"{path}: /.activity/ is read-only")
        return None
