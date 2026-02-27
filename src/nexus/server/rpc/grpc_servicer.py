"""VFS gRPC servicer — async handler for NexusVFSService.Call.

Mirrors the HTTP ``rpc_endpoint()`` in ``server/api/core/rpc.py`` but over
gRPC.  Reuses the same dispatch infrastructure: ``parse_method_params()``,
``dispatch_method()``, ``get_operation_context()``, ``rpc_codec``.

The servicer is generic — any gRPC client (REMOTE profile, federation
peers, CLI tools) can call it.  Application-level errors are returned
inside ``CallResponse.is_error=True`` with a JSON error dict payload,
keeping gRPC status codes for transport-level errors only.

Issue #1133: Unified gRPC transport.
Issue #1202: gRPC for REMOTE profile.
"""

from __future__ import annotations

import logging
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
    ValidationError,
)
from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc
from nexus.lib.rpc_codec import decode_rpc_message, encode_rpc_message
from nexus.server.protocol import RPCErrorCode, parse_method_params

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
        if self._api_key and token == self._api_key:
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
                return dict(result)

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
        from nexus.server.api.core.rpc import _scope_params_for_zone
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

            # 5. Zone scoping
            _scope_params_for_zone(params, op_context.zone_id)

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
