"""Boot helper for REMOTE deployment profile — the ``mount -t nfs`` command.

Fills NexusFS kernel service slots with RemoteServiceProxy instances,
forwarding all method calls to the server via the transport-agnostic
``call_rpc`` callback.

Additionally patches core filesystem methods (read, write, delete, …) to
forward as RPCs instead of executing the local metadata+backend flow.
The local flow assumes CAS etags, local metadata, and a local permission
enforcer — none of which exist in REMOTE mode.

Issue #1171: Service-layer RPC proxy for REMOTE profile.
Issue #844:  Part of NexusFS(profile=REMOTE) convergence.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)

# All fields accepted by NexusFS._bind_wired_services() (dict path).
# Derived from nexus_fs.py:290-326.
_WIRED_FIELDS: list[str] = [
    # Services
    "rebac_service",
    "mount_service",
    "gateway",
    "mount_core_service",
    "sync_service",
    "sync_job_service",
    "mount_persist_service",
    "mcp_service",
    "llm_service",
    "llm_subsystem",
    "oauth_service",
    "skill_service",
    "skill_package_service",
    "search_service",
    "share_link_service",
    "events_service",
    "task_queue_service",
    "workspace_rpc_service",
    "agent_rpc_service",
    "user_provisioning_service",
    "sandbox_rpc_service",
    "metadata_export_service",
    "ace_rpc_service",
    "descendant_checker",
    "memory_provider",
]


def _boot_remote_services(nfs: "NexusFS", call_rpc: Callable[..., Any]) -> None:
    """Wire RemoteServiceProxy instances as all service attributes.

    Like ``mount -t nfs``: fills VFS service slots with RPC forwarders
    instead of local service implementations.

    Called by ``connect(mode="remote")`` after NexusFS construction.

    Args:
        nfs: The NexusFS instance to wire services onto.
        call_rpc: Transport-agnostic RPC callback (today HTTP, future gRPC).
    """
    from nexus.remote.service_proxy import RemoteServiceProxy

    proxy = RemoteServiceProxy(call_rpc, service_name="universal")

    # Fill all wired service slots via _bind_wired_services (dict path)
    wired_dict: dict[str, Any] = dict.fromkeys(_WIRED_FIELDS, proxy)
    nfs._bind_wired_services(wired_dict)

    # BrickServices field not covered by WiredServices
    nfs.version_service = proxy

    # Patch core filesystem methods to forward as RPCs.
    # The local NexusFSCoreMixin flow assumes CAS etags, local metastore,
    # and a local permission enforcer — none of which exist in REMOTE mode.
    _patch_core_fs_methods(nfs, call_rpc)

    logger.info(
        "REMOTE profile: wired %d service slots + core FS methods with RPC forwarders",
        len(_WIRED_FIELDS) + 1,
    )


# ---------------------------------------------------------------------------
# Core filesystem RPC forwarders
# ---------------------------------------------------------------------------


def _patch_core_fs_methods(nfs: "NexusFS", call_rpc: Callable[..., Any]) -> None:
    """Replace core FS methods with RPC-forwarding stubs.

    Only called in REMOTE mode.  Each forwarder maps the Python method
    signature to the server's RPC parameter schema and normalises the
    response back to what callers expect.
    """
    import types

    def _rpc(method: str, params: dict[str, Any] | None = None) -> Any:
        return call_rpc(method, params)

    # -- read ---------------------------------------------------------------
    def read(
        self: Any,
        path: str,
        context: Any = None,
        return_metadata: bool = False,
        parsed: bool = False,
    ) -> bytes | dict[str, Any]:
        params: dict[str, Any] = {"path": path}
        if return_metadata:
            params["return_metadata"] = True
        if parsed:
            params["parsed"] = True
        result = _rpc("read", params)
        if isinstance(result, dict):
            content = result.get("content", b"")
            if isinstance(content, str):
                import base64

                try:
                    content = base64.b64decode(content)
                except Exception:
                    content = content.encode("utf-8")
            if return_metadata:
                result["content"] = content
                return result
            return bytes(content)
        if isinstance(result, str):
            return result.encode("utf-8")
        return bytes(result) if result else b""

    # -- write --------------------------------------------------------------
    def write(
        self: Any,
        path: str,
        content: bytes | str,
        context: Any = None,
        if_match: str | None = None,
        if_none_match: bool = False,
        force: bool = False,
        lock: bool = False,
        lock_timeout: float = 30.0,
        **_kw: Any,
    ) -> dict[str, Any]:
        if isinstance(content, str):
            content = content.encode("utf-8")
        params: dict[str, Any] = {"path": path, "content": content}
        if if_match:
            params["if_match"] = if_match
        if if_none_match:
            params["if_none_match"] = True
        if force:
            params["force"] = True
        if lock:
            params["lock"] = True
            params["lock_timeout"] = lock_timeout
        result = _rpc("write", params)
        if isinstance(result, dict):
            # Server handler returns {"bytes_written": <write_result_dict>}
            bw = result.get("bytes_written", result)
            if isinstance(bw, dict):
                return bw
            # Legacy: bytes_written is an int
            return {"bytes_written": bw, "size": bw}
        return {"bytes_written": result}

    # -- delete -------------------------------------------------------------
    def delete(self: Any, path: str, context: Any = None) -> dict[str, Any]:
        result = _rpc("delete", {"path": path})
        return result if isinstance(result, dict) else {"path": path}

    # -- exists -------------------------------------------------------------
    def exists(self: Any, path: str, context: Any = None) -> bool:
        result = _rpc("exists", {"path": path})
        if isinstance(result, dict):
            return bool(result.get("exists", False))
        return bool(result)

    # -- stat ---------------------------------------------------------------
    def stat(self: Any, path: str, context: Any = None) -> dict[str, Any]:
        result: dict[str, Any] = _rpc("stat", {"path": path})
        return result

    # -- list ---------------------------------------------------------------
    def list(
        self: Any,
        path: str = "/",
        context: Any = None,
        recursive: bool = False,
        cursor: str | None = None,
        limit: int | None = None,
        details: bool = False,
        show_parsed: bool | None = None,
        **_kw: Any,
    ) -> Any:
        params: dict[str, Any] = {"path": path}
        if recursive:
            params["recursive"] = True
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        if details:
            params["details"] = True
        if show_parsed is not None:
            params["show_parsed"] = show_parsed
        result = _rpc("list", params)
        if isinstance(result, dict):
            return result.get("files", [])
        return result if result else []

    # -- mkdir --------------------------------------------------------------
    def mkdir(
        self: Any,
        path: str,
        parents: bool = True,
        context: Any = None,
        **_kw: Any,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"path": path}
        if parents:
            params["parents"] = True
        result = _rpc("mkdir", params)
        return result if isinstance(result, dict) else {"created": True}

    # -- rmdir --------------------------------------------------------------
    def rmdir(
        self: Any,
        path: str,
        recursive: bool = False,
        force: bool = False,
        context: Any = None,
        **_kw: Any,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"path": path}
        if recursive:
            params["recursive"] = True
        if force:
            params["force"] = True
        result = _rpc("rmdir", params)
        return result if isinstance(result, dict) else {"removed": True}

    # -- rename -------------------------------------------------------------
    def rename(
        self: Any,
        old_path: str,
        new_path: str,
        context: Any = None,
        **_kw: Any,
    ) -> dict[str, Any]:
        result = _rpc("rename", {"old_path": old_path, "new_path": new_path})
        return result if isinstance(result, dict) else {"old_path": old_path, "new_path": new_path}

    # -- copy ---------------------------------------------------------------
    def copy(
        self: Any,
        source: str,
        destination: str,
        context: Any = None,
        **_kw: Any,
    ) -> dict[str, Any]:
        result = _rpc("copy", {"source": source, "destination": destination})
        return (
            result if isinstance(result, dict) else {"source": source, "destination": destination}
        )

    # -- is_directory -------------------------------------------------------
    def is_directory(self: Any, path: str, context: Any = None) -> bool:
        result = _rpc("is_directory", {"path": path})
        if isinstance(result, dict):
            return bool(result.get("is_directory", False))
        return bool(result)

    # -- get_metadata -------------------------------------------------------
    def get_metadata(self: Any, path: str, context: Any = None) -> dict[str, Any] | None:
        result = _rpc("get_metadata", {"path": path})
        if isinstance(result, dict):
            meta: dict[str, Any] | None = result.get("metadata", result)
            return meta
        return None

    # -- get_etag -----------------------------------------------------------
    def get_etag(self: Any, path: str, context: Any = None) -> str | None:
        try:
            result = _rpc("stat", {"path": path})
            if isinstance(result, dict):
                return result.get("etag")
        except Exception:
            pass
        return None

    # -- glob ---------------------------------------------------------------
    def glob(
        self: Any,
        pattern: str,
        path: str | None = None,
        context: Any = None,
        **_kw: Any,
    ) -> Any:
        params: dict[str, Any] = {"pattern": pattern}
        if path:
            params["path"] = path
        result = _rpc("glob", params)
        if isinstance(result, dict):
            return result.get("matches", [])
        return result if result else []

    # -- grep ---------------------------------------------------------------
    def grep(
        self: Any,
        pattern: str,
        path: str | None = None,
        context: Any = None,
        **_kw: Any,
    ) -> Any:
        params: dict[str, Any] = {"pattern": pattern}
        if path:
            params["path"] = path
        result = _rpc("grep", params)
        if isinstance(result, dict):
            return result.get("matches", [])
        return result if result else []

    # -- search -------------------------------------------------------------
    def search(
        self: Any,
        query: str,
        path: str | None = None,
        context: Any = None,
        **_kw: Any,
    ) -> Any:
        params: dict[str, Any] = {"query": query}
        if path:
            params["path"] = path
        result = _rpc("search", params)
        if isinstance(result, dict):
            return result.get("results", [])
        return result if result else []

    # -- Bind all forwarders to the NexusFS instance ------------------------
    for name, fn in [
        ("read", read),
        ("write", write),
        ("delete", delete),
        ("exists", exists),
        ("stat", stat),
        ("list", list),
        ("mkdir", mkdir),
        ("rmdir", rmdir),
        ("rename", rename),
        ("copy", copy),
        ("is_directory", is_directory),
        ("get_metadata", get_metadata),
        ("get_etag", get_etag),
        ("glob", glob),
        ("grep", grep),
        ("search", search),
    ]:
        setattr(nfs, name, types.MethodType(cast(Callable[..., Any], fn), nfs))
