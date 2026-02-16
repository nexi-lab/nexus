"""RPC dispatch infrastructure.

Extracted from fastapi_server.py (#1602). The dispatch layer routes RPC
method calls to domain-specific handler modules.

Key design decisions:
- Handlers are imported lazily inside ``build_dispatch_table()`` to avoid
  circular imports with ``fastapi_server.py``.
- ``dispatch_method()`` accepts ``nexus_fs``, ``exposed_methods``, and
  ``subscription_manager`` as explicit parameters (Issue #4A).
- ``fire_rpc_event()`` accepts ``subscription_manager`` as an explicit
  parameter instead of reaching into module-level globals.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True, slots=True)
class DispatchEntry:
    """Dispatch table entry for an RPC method.

    Attributes:
        handler: The handler callable — signature is (nexus_fs, params, context)
            for standard handlers, or (auth_provider, params, context) for admin.
        is_async: If True, handler is awaited directly; otherwise wrapped
            with ``to_thread_with_timeout``.
        event_type: If set, fire this event type after handler completes.
        event_path_attr: Attribute name on ``params`` for the event path.
        event_old_path_attr: Attribute name on ``params`` for old_path (rename).
        event_size_key: Key in the result dict to extract size (write).
        pass_auth_provider: If True, first arg is auth_provider instead of nexus_fs.
    """

    handler: Any
    is_async: bool = False
    event_type: str | None = None
    event_path_attr: str = "path"
    event_old_path_attr: str | None = None
    event_size_key: str | None = None
    pass_auth_provider: bool = False


# Lazily initialized — handler functions are imported on first RPC request.
_DISPATCH_TABLE: dict[str, DispatchEntry] = {}


def build_dispatch_table() -> dict[str, DispatchEntry]:
    """Build the RPC dispatch table.

    Called once on first RPC request. Handler modules are imported here
    (not at module level) to avoid circular imports with fastapi_server.py.
    """
    from nexus.server.rpc.handlers.admin import (
        handle_admin_create_key,
        handle_admin_get_key,
        handle_admin_list_keys,
        handle_admin_revoke_key,
        handle_admin_update_key,
    )
    from nexus.server.rpc.handlers.delta import (
        handle_delta_read,
        handle_delta_write,
    )
    from nexus.server.rpc.handlers.filesystem import (
        handle_copy,
        handle_delete,
        handle_exists,
        handle_get_metadata,
        handle_glob,
        handle_grep,
        handle_is_directory,
        handle_list,
        handle_mkdir,
        handle_read_async,
        handle_rename,
        handle_rmdir,
        handle_search,
        handle_semantic_search_index,
        handle_write,
    )
    from nexus.server.rpc.handlers.memory import (
        handle_approve_memory,
        handle_approve_memory_batch,
        handle_deactivate_memory,
        handle_deactivate_memory_batch,
        handle_delete_memory,
        handle_delete_memory_batch,
        handle_list_memories,
        handle_query_memories,
        handle_retrieve_memory,
        handle_store_memory,
    )

    return {
        # Core filesystem operations
        "read": DispatchEntry(handle_read_async, is_async=True),
        "write": DispatchEntry(
            handle_write, event_type="file_write", event_size_key="bytes_written"
        ),
        "exists": DispatchEntry(handle_exists),
        "list": DispatchEntry(handle_list),
        "delete": DispatchEntry(handle_delete, event_type="file_delete"),
        "rename": DispatchEntry(
            handle_rename,
            event_type="file_rename",
            event_path_attr="new_path",
            event_old_path_attr="old_path",
        ),
        "copy": DispatchEntry(handle_copy),
        "mkdir": DispatchEntry(handle_mkdir, event_type="dir_create"),
        "rmdir": DispatchEntry(handle_rmdir, event_type="dir_delete"),
        "get_metadata": DispatchEntry(handle_get_metadata),
        "glob": DispatchEntry(handle_glob),
        "grep": DispatchEntry(handle_grep),
        "search": DispatchEntry(handle_search),
        "is_directory": DispatchEntry(handle_is_directory),
        # Delta sync
        "delta_read": DispatchEntry(handle_delta_read),
        "delta_write": DispatchEntry(handle_delta_write),
        # Semantic search
        "semantic_search_index": DispatchEntry(handle_semantic_search_index, is_async=True),
        # Memory API
        "store_memory": DispatchEntry(handle_store_memory),
        "list_memories": DispatchEntry(handle_list_memories),
        "query_memories": DispatchEntry(handle_query_memories),
        "retrieve_memory": DispatchEntry(handle_retrieve_memory),
        "delete_memory": DispatchEntry(handle_delete_memory),
        "approve_memory": DispatchEntry(handle_approve_memory),
        "deactivate_memory": DispatchEntry(handle_deactivate_memory),
        "approve_memory_batch": DispatchEntry(handle_approve_memory_batch),
        "deactivate_memory_batch": DispatchEntry(handle_deactivate_memory_batch),
        "delete_memory_batch": DispatchEntry(handle_delete_memory_batch),
        # Admin API
        "admin_create_key": DispatchEntry(handle_admin_create_key, pass_auth_provider=True),
        "admin_list_keys": DispatchEntry(handle_admin_list_keys, pass_auth_provider=True),
        "admin_get_key": DispatchEntry(handle_admin_get_key, pass_auth_provider=True),
        "admin_revoke_key": DispatchEntry(handle_admin_revoke_key, pass_auth_provider=True),
        "admin_update_key": DispatchEntry(handle_admin_update_key, pass_auth_provider=True),
    }


async def fire_rpc_event(
    subscription_manager: Any,
    event_type: str,
    path: str,
    context: Any,
    old_path: str | None = None,
    size: int | None = None,
) -> None:
    """Fire an event after RPC mutation operation (non-blocking).

    Args:
        subscription_manager: The subscription manager instance (may be None).
        event_type: Event type (file_write, file_delete, etc.)
        path: File/directory path
        context: Request context with zone info
        old_path: Old path for rename operations
        size: File size for write operations
    """
    if not subscription_manager:
        return

    try:
        zone_id = getattr(context, "zone_id", None) or "default"
        data: dict[str, Any] = {"file_path": path}
        if old_path:
            data["old_path"] = old_path
        if size is not None:
            data["size"] = size

        await subscription_manager.broadcast(event_type, data, zone_id)
    except Exception as e:
        logger.warning(f"[RPC] Failed to fire event {event_type} for {path}: {e}")


async def dispatch_method(
    method: str,
    params: Any,
    context: Any,
    *,
    nexus_fs: Any,
    exposed_methods: dict[str, Any],
    auth_provider: Any = None,
    subscription_manager: Any = None,
) -> Any:
    """Dispatch RPC method call.

    Looks up the method in the dispatch table first, then falls back to
    ``auto_dispatch`` for dynamically exposed methods.

    Args:
        method: RPC method name
        params: Parsed method parameters
        context: Operation context
        nexus_fs: NexusFS instance
        exposed_methods: Dict of dynamically exposed methods
        auth_provider: Auth provider for admin handlers
        subscription_manager: For firing mutation events
    """
    global _DISPATCH_TABLE  # noqa: PLW0603

    if nexus_fs is None:
        raise RuntimeError("NexusFS not initialized")

    # Lazy-init on first call
    if not _DISPATCH_TABLE:
        _DISPATCH_TABLE = build_dispatch_table()

    # Issue #1457: Enforce admin_only for ALL dispatch paths
    func = exposed_methods.get(method)
    if func and getattr(func, "_rpc_admin_only", False):
        from nexus.server.rpc.handlers.admin import require_admin

        require_admin(context)

    # Auto-dispatch takes priority for dynamically exposed methods
    # that are NOT in the static dispatch table
    if method in exposed_methods and method not in _DISPATCH_TABLE:
        return await _auto_dispatch(method, params, context, exposed_methods=exposed_methods)

    entry = _DISPATCH_TABLE.get(method)
    if entry is not None:
        from nexus.server.fastapi_server import to_thread_with_timeout

        # Determine the first argument (nexus_fs or auth_provider)
        first_arg = auth_provider if entry.pass_auth_provider else nexus_fs

        # Execute handler
        if entry.is_async:
            result = await entry.handler(first_arg, params, context)
        else:
            result = await to_thread_with_timeout(entry.handler, first_arg, params, context)

        # Fire subscription event for mutations
        if entry.event_type is not None:
            path = getattr(params, entry.event_path_attr, None)
            old_path = (
                getattr(params, entry.event_old_path_attr, None)
                if entry.event_old_path_attr
                else None
            )
            size = (
                result.get(entry.event_size_key)
                if entry.event_size_key and isinstance(result, dict)
                else None
            )
            await fire_rpc_event(
                subscription_manager,
                entry.event_type,
                path or "",
                context,
                old_path=old_path,
                size=size,
            )

        return result

    # Fallback: try auto-dispatch for exposed methods
    if method in exposed_methods:
        return await _auto_dispatch(method, params, context, exposed_methods=exposed_methods)

    raise ValueError(f"Unknown method: {method}")


async def _auto_dispatch(
    method: str,
    params: Any,
    context: Any,
    *,
    exposed_methods: dict[str, Any],
) -> Any:
    """Auto-dispatch to exposed method."""
    import inspect

    from nexus.server.fastapi_server import to_thread_with_timeout

    func = exposed_methods[method]

    kwargs: dict[str, Any] = {}
    sig = inspect.signature(func)

    for param_name, _param in sig.parameters.items():
        if param_name == "self":
            continue
        elif param_name in ("context", "_context"):
            kwargs[param_name] = context
        elif hasattr(params, param_name):
            kwargs[param_name] = getattr(params, param_name)

    if asyncio.iscoroutinefunction(func):
        return await func(**kwargs)
    else:
        timeout = 300.0 if method == "sync_mount" else None
        return await to_thread_with_timeout(func, timeout=timeout, **kwargs)
