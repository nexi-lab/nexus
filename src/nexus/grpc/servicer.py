"""VFS gRPC dispatcher — sync bridge from Rust tonic server to Python.

After Phase 1 of the VFS server migration, the Rust tonic server (in
``nexus_kernel``) owns ``:2028``. ``Read`` / ``Write`` / ``Delete`` /
``Ping`` are pure-Rust handlers that call ``Kernel::sys_*`` directly —
zero PyO3 cost. The generic ``Call`` RPC still requires Python because
its 195 ``@rpc_expose`` targets live in the Python ``bricks/services/``
tree.

This module provides the Python callables the Rust server invokes for
``Call``:

* ``VFSCallDispatcher.authenticate_sync(token)`` — validates a Bearer
  token. Returns the auth-result dict (or ``None``). Includes the
  static-API-key fast path AND the OIDC ``auth_provider`` async path
  (bridged via ``run_coroutine_threadsafe`` onto the FastAPI loop).

* ``VFSCallDispatcher.dispatch_call_sync(method, payload, auth_dict)``
  — runs the existing async ``dispatch_method`` on the FastAPI loop,
  blocks for the JSON-encoded result, returns ``(is_error, payload)``.

Mirrors the dispatch shape of ``server.api.core.rpc.rpc_endpoint`` but
without any async/HTTP framework — purely a sync function the Rust
server calls under ``tokio::task::spawn_blocking + Python::with_gil``.

Issue #1133 / #1202 / #1249 (transport unification + REMOTE profile +
port consolidation) all preserved — the wire contract is unchanged,
only the server impl moved.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hmac
import logging
from types import SimpleNamespace
from typing import Any

from nexus.contracts.exceptions import NexusError
from nexus.contracts.rpc_types import RPCErrorCode
from nexus.lib.rpc_codec import decode_rpc_message, encode_rpc_message
from nexus.lib.zone_scoping import ZoneScopingError, scope_params_for_zone
from nexus.server.protocol import parse_method_params

logger = logging.getLogger(__name__)


def _error_payload(code: "RPCErrorCode", message: str, data: dict | None = None) -> bytes:
    """Build a JSON-encoded error dict matching the JSON-RPC error format."""
    err: dict[str, Any] = {
        "code": code.value if hasattr(code, "value") else code,
        "message": message,
    }
    if data:
        err["data"] = data
    return encode_rpc_message(err)


class VFSCallDispatcher:
    """Sync bridge between the Rust tonic server and Python's async dispatch.

    Created once at FastAPI startup; callables are passed by reference
    to ``nexus_kernel.start_vfs_grpc_server``. Holds:

    - ``loop`` — the FastAPI event loop. ``dispatch_method`` and
      ``auth_provider.authenticate`` are async; the Rust server calls
      our sync wrappers from a tokio blocking thread, so we schedule
      back onto this loop and block.
    - ``nexus_fs`` / ``exposed_methods`` / ``auth_provider`` /
      ``subscription_manager`` — passed through to ``dispatch_method``,
      same surface as the old ``VFSServicer.Call`` had.
    """

    def __init__(
        self,
        nexus_fs: Any,
        exposed_methods: dict[str, Any],
        auth_provider: Any = None,
        api_key: str | None = None,
        subscription_manager: Any = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._nexus_fs = nexus_fs
        self._exposed_methods = exposed_methods
        self._auth_provider = auth_provider
        self._api_key = api_key
        self._subscription_manager = subscription_manager
        self._loop = loop or asyncio.get_event_loop()

    # ------------------------------------------------------------------
    # Authentication — sync from Rust caller's view
    # ------------------------------------------------------------------

    async def _authenticate(self, token: str) -> dict[str, Any]:
        """Validate Bearer token and return auth-result dict (or empty).

        Mirrors the original ``VFSServicer._authenticate`` for the
        OIDC path; the static-API-key fast path also lives in Rust
        (``grpc_server.rs::VfsServiceImpl::resolve_context``) for
        ``Read`` / ``Write`` / ``Delete`` / ``Ping``. We keep both
        paths here because the Rust server only short-circuits for
        the static-key match — OIDC tokens reach this method.
        """
        from nexus.contracts.constants import ROOT_ZONE_ID

        if not token:
            if not self._api_key and not self._auth_provider:
                return {
                    "authenticated": True,
                    "subject_type": "user",
                    "subject_id": "anonymous",
                    "zone_id": ROOT_ZONE_ID,
                    "is_admin": False,
                }
            return {}

        if self._api_key and hmac.compare_digest(token, self._api_key):
            return {
                "authenticated": True,
                "subject_type": "user",
                "subject_id": "api-key-user",
                "zone_id": ROOT_ZONE_ID,
                "is_admin": True,
            }

        if self._auth_provider:
            result = await self._auth_provider.authenticate(token)
            if result:
                return (
                    dataclasses.asdict(result)
                    if hasattr(result, "__dataclass_fields__")
                    else dict(result)
                )

        return {}

    def authenticate_sync(self, token: str) -> dict[str, Any] | None:
        """Sync entry point invoked by Rust server under spawn_blocking + GIL."""
        try:
            future = asyncio.run_coroutine_threadsafe(self._authenticate(token), self._loop)
            result = future.result(timeout=30)
            if result and result.get("authenticated"):
                return result
            return None
        except Exception as exc:
            logger.warning("authenticate_sync: error: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Call dispatch — sync from Rust caller's view
    # ------------------------------------------------------------------

    def dispatch_call_sync(
        self,
        method: str,
        payload: bytes,
        auth_dict: dict[str, Any],
    ) -> tuple[bool, bytes]:
        """Run dispatch_method on the FastAPI loop, return (is_error, payload).

        The tuple shape matches what the Rust server expects in
        ``grpc_server::call``. Errors are encoded as JSON-RPC error
        dicts so the Python contract on the wire is identical to the
        old ``VFSServicer.Call``.
        """
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._dispatch_async(method, payload, auth_dict),
                self._loop,
            )
            return future.result(timeout=300)
        except TimeoutError:
            return True, _error_payload(RPCErrorCode.INTERNAL_ERROR, "Dispatch timeout")
        except Exception as exc:
            logger.exception("dispatch_call_sync: unexpected error")
            return True, _error_payload(RPCErrorCode.INTERNAL_ERROR, f"Internal error: {exc}")

    async def _dispatch_async(
        self,
        method: str,
        payload: bytes,
        auth_dict: dict[str, Any],
    ) -> tuple[bool, bytes]:
        from nexus.server.dependencies import get_operation_context
        from nexus.server.rpc.dispatch import dispatch_method

        try:
            params_dict = decode_rpc_message(payload) if payload else {}

            try:
                params = parse_method_params(method, params_dict)
            except ValueError:
                if method in self._exposed_methods:
                    params = SimpleNamespace(**params_dict)
                else:
                    raise

            op_context = get_operation_context(auth_dict)

            search_delegation = auth_dict.get("search_delegation")
            if search_delegation is not None:
                target_zone = op_context.zone_id or ""
                try:
                    search_delegation.validate(method, target_zone)
                except PermissionError as perm_err:
                    return True, _error_payload(RPCErrorCode.PERMISSION_ERROR, str(perm_err))
            else:
                scope_params_for_zone(params, op_context.zone_id)

            result = await dispatch_method(
                method,
                params,
                op_context,
                nexus_fs=self._nexus_fs,
                exposed_methods=self._exposed_methods,
                auth_provider=self._auth_provider,
                subscription_manager=self._subscription_manager,
            )
            return False, encode_rpc_message({"result": result})

        except ZoneScopingError as exc:
            return True, _error_payload(RPCErrorCode.PERMISSION_ERROR, str(exc))
        except ValueError as exc:
            return True, _error_payload(RPCErrorCode.INVALID_PARAMS, f"Invalid parameters: {exc}")
        except NexusError as exc:
            return True, _error_payload(RPCErrorCode.INTERNAL_ERROR, str(exc))
        except Exception as exc:
            logger.exception("dispatch_call_sync: unexpected error in %s", method)
            return True, _error_payload(RPCErrorCode.INTERNAL_ERROR, f"Internal error: {exc}")
