"""gRPC servicer — async handlers for NexusVFSService.

Phase 1 (PR #2667): Generic ``Call`` RPC — method name + JSON payload.
Phase 2: Typed RPCs for content ops (Read/Write/Delete) with
native ``bytes`` fields (no base64) and a ``Ping`` health check with
server metadata.

Mirrors the HTTP ``rpc_endpoint()`` in ``server/api/core/rpc.py`` but over
gRPC.  Reuses the same dispatch infrastructure: ``parse_method_params()``,
``dispatch_method()``, ``get_operation_context()``, ``rpc_codec``.

The servicer is generic — any gRPC client (REMOTE profile, federation
peers, CLI tools) can call it.  Application-level errors are returned
inside response ``is_error`` / ``error_payload`` fields, keeping gRPC
status codes for transport-level errors only.

Issue #1133: Unified gRPC transport.
Issue #1202: gRPC for REMOTE profile.
Issue #1249: Port consolidation — single gRPC server.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hmac
import logging
import time
from types import SimpleNamespace
from typing import Any

import grpc

from nexus.contracts.exceptions import (
    ConflictError,
    ConnectorError,
    DatabaseError,
    InvalidPathError,
    NexusError,
    NexusFileNotFoundError,
    NexusPermissionError,
    PathNotMountedError,
    ValidationError,
)
from nexus.contracts.rpc_types import RPCErrorCode
from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc
from nexus.lib.rpc_codec import decode_rpc_message, encode_rpc_message
from nexus.lib.zone_scoping import ZoneScopingError, scope_params_for_zone
from nexus.server.protocol import parse_method_params

logger = logging.getLogger(__name__)

# Captured at import time — used by Ping to report uptime.
_SERVER_START_TIME = time.monotonic()


def _error_payload(code: "RPCErrorCode", message: str, data: dict | None = None) -> bytes:
    """Build a JSON-encoded error dict matching the JSON-RPC error format."""
    err: dict[str, Any] = {
        "code": code.value if hasattr(code, "value") else code,
        "message": message,
    }
    if data:
        err["data"] = data
    return encode_rpc_message(err)


class VFSServicer(vfs_pb2_grpc.NexusVFSServiceServicer):
    """Async gRPC servicer for the NexusVFSService.

    Args:
        nexus_fs: The NexusFS kernel instance.
        exposed_methods: Dict of dynamically exposed ``@rpc_expose`` methods.
        auth_provider: Optional auth provider for token validation.
        api_key: Optional static API key for Bearer-token auth.
        subscription_manager: Optional subscription manager for mutation events.
    """

    def __init__(
        self,
        nexus_fs: Any,
        exposed_methods: dict[str, Any],
        auth_provider: Any = None,
        api_key: str | None = None,
        subscription_manager: Any = None,
    ) -> None:
        self._nexus_fs = nexus_fs
        self._exposed_methods = exposed_methods
        self._auth_provider = auth_provider
        self._api_key = api_key
        self._subscription_manager = subscription_manager

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def _authenticate(self, token: str) -> dict[str, Any]:
        """Validate Bearer token and return auth result dict.

        Mirrors the logic in ``server/dependencies.py:require_auth``.
        """
        from nexus.contracts.constants import ROOT_ZONE_ID

        if not token:
            # No token — open access mode
            if not self._api_key and not self._auth_provider:
                return {
                    "authenticated": True,
                    "subject_type": "user",
                    "subject_id": "anonymous",
                    "zone_id": ROOT_ZONE_ID,
                    "is_admin": False,
                }
            return {}  # Will fail later

        # Static API key check
        if self._api_key and hmac.compare_digest(token, self._api_key):
            return {
                "authenticated": True,
                "subject_type": "user",
                "subject_id": "api-key-user",
                "zone_id": ROOT_ZONE_ID,
                "is_admin": True,
            }

        # Auth provider
        if self._auth_provider:
            result = await self._auth_provider.authenticate(token)
            if result:
                return (
                    dataclasses.asdict(result)
                    if hasattr(result, "__dataclass_fields__")
                    else dict(result)
                )

        return {}

    # ------------------------------------------------------------------
    # RPC handler
    # ------------------------------------------------------------------

    async def Call(
        self,
        request: "vfs_pb2.CallRequest",
        _context: grpc.aio.ServicerContext,
    ) -> "vfs_pb2.CallResponse":
        """Handle a generic VFS RPC call."""
        from nexus.server.dependencies import get_operation_context
        from nexus.server.rpc.dispatch import dispatch_method

        method = request.method

        try:
            # 1. Auth
            auth_result = await self._authenticate(request.auth_token)
            if not auth_result.get("authenticated") and (self._api_key or self._auth_provider):
                return vfs_pb2.CallResponse(
                    payload=_error_payload(RPCErrorCode.ACCESS_DENIED, "Authentication required"),
                    is_error=True,
                )

            # 2. Decode params
            params_dict = decode_rpc_message(request.payload) if request.payload else {}

            # 3. Parse method params
            try:
                params = parse_method_params(method, params_dict)
            except ValueError:
                if method in self._exposed_methods:
                    params = SimpleNamespace(**params_dict)
                else:
                    raise

            # 4. Operation context
            op_context = get_operation_context(auth_result)

            # 5. SearchDelegation guard (Issue #3147, Phase 2)
            search_delegation = auth_result.get("search_delegation")
            if search_delegation is not None:
                target_zone = op_context.zone_id or ""
                try:
                    search_delegation.validate(method, target_zone)
                except PermissionError as perm_err:
                    return vfs_pb2.CallResponse(
                        payload=_error_payload(
                            RPCErrorCode.PERMISSION_ERROR,
                            str(perm_err),
                        ),
                        is_error=True,
                    )
            else:
                # Normal auth — zone scoping as today
                scope_params_for_zone(params, op_context.zone_id)

            # 6. Dispatch
            result = await dispatch_method(
                method,
                params,
                op_context,
                nexus_fs=self._nexus_fs,
                exposed_methods=self._exposed_methods,
                auth_provider=self._auth_provider,
                subscription_manager=self._subscription_manager,
            )

            # 7. Encode success response
            return vfs_pb2.CallResponse(
                payload=encode_rpc_message({"result": result}),
                is_error=False,
            )

        except ZoneScopingError as e:
            return vfs_pb2.CallResponse(
                payload=_error_payload(RPCErrorCode.PERMISSION_ERROR, str(e)),
                is_error=True,
            )
        except ValueError as e:
            return vfs_pb2.CallResponse(
                payload=_error_payload(RPCErrorCode.INVALID_PARAMS, f"Invalid parameters: {e}"),
                is_error=True,
            )
        except NexusFileNotFoundError as e:
            return vfs_pb2.CallResponse(
                payload=_error_payload(RPCErrorCode.FILE_NOT_FOUND, str(e)),
                is_error=True,
            )
        except InvalidPathError as e:
            return vfs_pb2.CallResponse(
                payload=_error_payload(RPCErrorCode.INVALID_PATH, str(e)),
                is_error=True,
            )
        except NexusPermissionError as e:
            return vfs_pb2.CallResponse(
                payload=_error_payload(RPCErrorCode.PERMISSION_ERROR, str(e)),
                is_error=True,
            )
        except ValidationError as e:
            return vfs_pb2.CallResponse(
                payload=_error_payload(RPCErrorCode.VALIDATION_ERROR, str(e)),
                is_error=True,
            )
        except ConflictError as e:
            return vfs_pb2.CallResponse(
                payload=_error_payload(
                    RPCErrorCode.CONFLICT,
                    str(e),
                    data={
                        "path": e.path,
                        "expected_etag": e.expected_etag,
                        "current_etag": e.current_etag,
                    },
                ),
                is_error=True,
            )
        except DatabaseError as e:
            logger.warning("Database error in gRPC method %s: %s", method, e)
            return vfs_pb2.CallResponse(
                payload=_error_payload(RPCErrorCode.INTERNAL_ERROR, f"Database error: {e}"),
                is_error=True,
            )
        except ConnectorError as e:
            logger.warning("Connector error in gRPC method %s: %s", method, e)
            return vfs_pb2.CallResponse(
                payload=_error_payload(RPCErrorCode.INTERNAL_ERROR, f"Backend error: {e}"),
                is_error=True,
            )
        except PathNotMountedError as e:
            logger.warning("PathNotMountedError in gRPC method %s: %s", method, e)
            return vfs_pb2.CallResponse(
                payload=_error_payload(RPCErrorCode.FILE_NOT_FOUND, f"Path not mounted: {e}"),
                is_error=True,
            )
        except NexusError as e:
            logger.warning("NexusError in gRPC method %s: %s", method, e)
            return vfs_pb2.CallResponse(
                payload=_error_payload(RPCErrorCode.INTERNAL_ERROR, f"Nexus error: {e}"),
                is_error=True,
            )
        except Exception as e:
            logger.exception("Error executing gRPC method %s", method)
            return vfs_pb2.CallResponse(
                payload=_error_payload(RPCErrorCode.INTERNAL_ERROR, f"Internal error: {e}"),
                is_error=True,
            )

    # ------------------------------------------------------------------
    # Shared auth + context pipeline for typed RPCs
    # ------------------------------------------------------------------

    async def _auth_and_context(self, auth_token: str) -> tuple[dict[str, Any], Any]:
        """Authenticate and build OperationContext — shared by all typed RPCs."""
        from nexus.server.dependencies import get_operation_context

        auth_result = await self._authenticate(auth_token)
        if not auth_result.get("authenticated") and (self._api_key or self._auth_provider):
            raise NexusPermissionError("Authentication required")
        return auth_result, get_operation_context(auth_result)

    # ------------------------------------------------------------------
    # Typed RPCs — content operations (Phase 2)
    # ------------------------------------------------------------------

    @staticmethod
    def _scope_path_for_zone(request: Any, zone_id: str) -> None:
        """Apply zone-scoping to typed gRPC request paths.

        Delegates to the shared zone_scoping module (Issue #3063).
        """
        scope_params_for_zone(request, zone_id)

    async def Read(
        self,
        request: "vfs_pb2.ReadRequest",
        _context: grpc.aio.ServicerContext,
    ) -> "vfs_pb2.ReadResponse":
        """Typed read — returns raw bytes, no JSON/base64 overhead.

        Always routes through sys_read (full VFS path) so that:
        1. Non-root zone callers route correctly — sys_read uses the kernel's
           ROOT_ZONE_ID for mount LPM, avoiding PathNotMountedError on paths
           like /zone/default/... which don't match the root "/" mount when
           the caller's zone_id is used for canonicalization.
        2. Path-addressed (PAS / path_local) backends work — they require
           backend_path in context, not a CAS hash as content_id.
        3. Permission hooks run — sys_read dispatches PermissionCheckHook
           so ReBAC enforcement applies to every read.
        """
        try:
            _, op_context = await self._auth_and_context(request.auth_token)
            self._scope_path_for_zone(request, op_context.zone_id)
            content = self._nexus_fs.sys_read(request.path, context=op_context)
            return vfs_pb2.ReadResponse(content=content, size=len(content))
        except ZoneScopingError as e:
            return vfs_pb2.ReadResponse(
                is_error=True,
                error_payload=_error_payload(RPCErrorCode.PERMISSION_ERROR, str(e)),
            )
        except NexusPermissionError as e:
            return vfs_pb2.ReadResponse(
                is_error=True,
                error_payload=_error_payload(RPCErrorCode.PERMISSION_ERROR, str(e)),
            )
        except NexusFileNotFoundError as e:
            return vfs_pb2.ReadResponse(
                is_error=True,
                error_payload=_error_payload(RPCErrorCode.FILE_NOT_FOUND, str(e)),
            )
        except InvalidPathError as e:
            return vfs_pb2.ReadResponse(
                is_error=True,
                error_payload=_error_payload(RPCErrorCode.INVALID_PATH, str(e)),
            )
        except NexusError as e:
            logger.warning("NexusError in gRPC Read %s: %s", request.path, e)
            return vfs_pb2.ReadResponse(
                is_error=True,
                error_payload=_error_payload(RPCErrorCode.INTERNAL_ERROR, str(e)),
            )
        except Exception as e:
            logger.exception("Error in gRPC Read %s", request.path)
            return vfs_pb2.ReadResponse(
                is_error=True,
                error_payload=_error_payload(RPCErrorCode.INTERNAL_ERROR, f"Internal error: {e}"),
            )

    async def Write(
        self,
        request: "vfs_pb2.WriteRequest",
        _context: grpc.aio.ServicerContext,
    ) -> "vfs_pb2.WriteResponse":
        """Typed write — accepts raw bytes, no JSON/base64 overhead.

        Uses write() (Tier 2) instead of sys_write() to get metadata back
        (etag, version, size).  OCC is handled via occ_write() when the
        client sends an etag — matching the JSON RPC handler pattern.

        Issue #2787: sys_write() returns int (POSIX), not dict.
        """
        try:
            _, op_context = await self._auth_and_context(request.auth_token)
            self._scope_path_for_zone(request, op_context.zone_id)
            content = bytes(request.content)
            if request.etag:
                # OCC: compare-and-swap via lib helper (Issue #1323)
                from nexus.lib.occ import occ_write

                result = await occ_write(
                    self._nexus_fs,
                    request.path,
                    content,
                    context=op_context,
                    if_match=request.etag,
                )
            else:
                result = self._nexus_fs.write(request.path, content, context=op_context)

            etag = result.get("etag", "") if isinstance(result, dict) else ""
            size = result.get("size", len(content)) if isinstance(result, dict) else len(content)
            return vfs_pb2.WriteResponse(etag=etag, size=size)
        except ZoneScopingError as e:
            return vfs_pb2.WriteResponse(
                is_error=True,
                error_payload=_error_payload(RPCErrorCode.PERMISSION_ERROR, str(e)),
            )
        except NexusPermissionError as e:
            return vfs_pb2.WriteResponse(
                is_error=True,
                error_payload=_error_payload(RPCErrorCode.PERMISSION_ERROR, str(e)),
            )
        except ConflictError as e:
            return vfs_pb2.WriteResponse(
                is_error=True,
                error_payload=_error_payload(
                    RPCErrorCode.CONFLICT,
                    str(e),
                    data={
                        "path": e.path,
                        "expected_etag": e.expected_etag,
                        "current_etag": e.current_etag,
                    },
                ),
            )
        except NexusFileNotFoundError as e:
            return vfs_pb2.WriteResponse(
                is_error=True,
                error_payload=_error_payload(RPCErrorCode.FILE_NOT_FOUND, str(e)),
            )
        except InvalidPathError as e:
            return vfs_pb2.WriteResponse(
                is_error=True,
                error_payload=_error_payload(RPCErrorCode.INVALID_PATH, str(e)),
            )
        except ValidationError as e:
            return vfs_pb2.WriteResponse(
                is_error=True,
                error_payload=_error_payload(RPCErrorCode.VALIDATION_ERROR, str(e)),
            )
        except NexusError as e:
            logger.warning("NexusError in gRPC Write %s: %s", request.path, e)
            return vfs_pb2.WriteResponse(
                is_error=True,
                error_payload=_error_payload(RPCErrorCode.INTERNAL_ERROR, str(e)),
            )
        except Exception as e:
            logger.exception("Error in gRPC Write %s", request.path)
            return vfs_pb2.WriteResponse(
                is_error=True,
                error_payload=_error_payload(RPCErrorCode.INTERNAL_ERROR, f"Internal error: {e}"),
            )

    async def Delete(
        self,
        request: "vfs_pb2.DeleteRequest",
        _context: grpc.aio.ServicerContext,
    ) -> "vfs_pb2.DeleteResponse":
        """Typed delete — sys_unlink or rmdir."""
        try:
            _, op_context = await self._auth_and_context(request.auth_token)
            self._scope_path_for_zone(request, op_context.zone_id)

            # Determine entry type so directories always go through rmdir
            # (sys_unlink skips backend directory cleanup).
            meta = await asyncio.to_thread(self._nexus_fs.metadata.get, request.path)
            is_dir = meta is not None and getattr(meta, "mime_type", "") == "inode/directory"

            if is_dir:
                self._nexus_fs.rmdir(
                    request.path,
                    recursive=request.recursive,
                    context=op_context,
                )
            else:
                self._nexus_fs.sys_unlink(request.path, context=op_context)
            return vfs_pb2.DeleteResponse(success=True)
        except ZoneScopingError as e:
            return vfs_pb2.DeleteResponse(
                is_error=True,
                error_payload=_error_payload(RPCErrorCode.PERMISSION_ERROR, str(e)),
            )
        except NexusPermissionError as e:
            return vfs_pb2.DeleteResponse(
                is_error=True,
                error_payload=_error_payload(RPCErrorCode.PERMISSION_ERROR, str(e)),
            )
        except NexusFileNotFoundError as e:
            return vfs_pb2.DeleteResponse(
                is_error=True,
                error_payload=_error_payload(RPCErrorCode.FILE_NOT_FOUND, str(e)),
            )
        except InvalidPathError as e:
            return vfs_pb2.DeleteResponse(
                is_error=True,
                error_payload=_error_payload(RPCErrorCode.INVALID_PATH, str(e)),
            )
        except NexusError as e:
            logger.warning("NexusError in gRPC Delete %s: %s", request.path, e)
            return vfs_pb2.DeleteResponse(
                is_error=True,
                error_payload=_error_payload(RPCErrorCode.INTERNAL_ERROR, str(e)),
            )
        except Exception as e:
            logger.exception("Error in gRPC Delete %s", request.path)
            return vfs_pb2.DeleteResponse(
                is_error=True,
                error_payload=_error_payload(RPCErrorCode.INTERNAL_ERROR, f"Internal error: {e}"),
            )

    async def Ping(
        self,
        request: "vfs_pb2.PingRequest",
        _context: grpc.aio.ServicerContext,
    ) -> "vfs_pb2.PingResponse":
        """Health check with server metadata."""
        import contextlib

        import nexus
        from nexus.contracts.constants import ROOT_ZONE_ID

        # Optionally validate auth (non-fatal for Ping — version/zone_id are public)
        with contextlib.suppress(NexusPermissionError):
            await self._auth_and_context(request.auth_token)

        zone_id = ROOT_ZONE_ID
        uptime = int(time.monotonic() - _SERVER_START_TIME)
        return vfs_pb2.PingResponse(
            version=nexus.__version__,
            zone_id=zone_id,
            uptime_seconds=uptime,
        )
