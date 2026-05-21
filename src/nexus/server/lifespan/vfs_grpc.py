"""VFS gRPC server lifespan for the public Nexus RPC surface."""

from __future__ import annotations

import logging
import os
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import grpc

from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc
from nexus.lib.rpc_codec import decode_rpc_message, encode_rpc_message
from nexus.lib.zone_scoping import scope_params_for_zone
from nexus.runtime.zone_resolution import target_zone_for_context
from nexus.server.dependencies import get_operation_context, resolve_auth
from nexus.server.protocol import RPCErrorCode, parse_method_params
from nexus.server.zone_execution import context_for_target_zone, run_zone_scoped

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _error_payload(code: RPCErrorCode, message: str, data: dict[str, Any] | None = None) -> bytes:
    error: dict[str, Any] = {"code": code.value, "message": message}
    if data:
        error["data"] = data
    return encode_rpc_message(error)


def _coerce_bytes(result: Any) -> bytes:
    if isinstance(result, bytes):
        return result
    if isinstance(result, str):
        return result.encode()
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, bytes):
            return content
        if isinstance(content, str):
            return content.encode()
    return b""


def _client_host_from_peer(peer: str | None) -> str | None:
    if not peer:
        return None
    if peer.startswith("ipv4:"):
        host_port = peer.removeprefix("ipv4:")
        host, _, _port = host_port.rpartition(":")
        return host or host_port
    if peer.startswith("ipv6:"):
        host_port = peer.removeprefix("ipv6:")
        host, _, _port = host_port.rpartition(":")
        return host.strip("[]") or host_port.strip("[]")
    return None


def _client_host_from_context(context: grpc.aio.ServicerContext) -> str | None:
    try:
        return _client_host_from_peer(context.peer())
    except Exception:
        return None


class VFSGrpcServicer(vfs_pb2_grpc.NexusVFSServiceServicer):
    def __init__(self, app: "FastAPI") -> None:
        self._app = app
        self._started = time.monotonic()

    async def _auth(self, token: str, *, client_host: str | None = None) -> dict[str, Any]:
        authorization = token if token.startswith(("Bearer ", "sk-")) else f"Bearer {token}"
        auth = await resolve_auth(
            self._app.state,
            authorization=authorization,
            client_host=client_host,
        )
        if auth is None or not auth.get("authenticated"):
            raise PermissionError("Invalid or missing API key")
        return auth

    async def _dispatch(
        self,
        method: str,
        params: dict[str, Any],
        token: str,
        *,
        client_host: str | None = None,
    ) -> Any:
        from nexus.server._kernel_syscall_dispatch import (
            KERNEL_SYSCALL_NAMES,
            dispatch_kernel_syscall,
        )
        from nexus.server.rpc.dispatch import dispatch_method

        state = self._app.state
        auth = await self._auth(token, client_host=client_host)
        context = get_operation_context(auth)

        if method in KERNEL_SYSCALL_NAMES:
            scoped = SimpleNamespace(**params)
            scope_params_for_zone(scoped, context.zone_id)
            scoped_params = vars(scoped)
            target_zone = target_zone_for_context(context, scoped_params)
            context = context_for_target_zone(context, target_zone)

            async def kernel_work() -> Any:
                return await dispatch_kernel_syscall(
                    state.nexus_fs,
                    method,
                    scoped_params,
                    context,
                    subscription_manager=state.subscription_manager,
                )

            return await run_zone_scoped(
                getattr(state, "zone_registry", None), target_zone, kernel_work
            )

        try:
            parsed = parse_method_params(method, params)
        except ValueError:
            exposed = getattr(state, "exposed_methods", {})
            if method not in exposed:
                raise
            parsed = SimpleNamespace(**params)

        scope_params_for_zone(parsed, context.zone_id)
        target_zone = target_zone_for_context(context, parsed)
        context = context_for_target_zone(context, target_zone)

        async def rpc_work() -> Any:
            return await dispatch_method(
                method,
                parsed,
                context,
                nexus_fs=state.nexus_fs,
                exposed_methods=state.exposed_methods,
                auth_provider=state.auth_provider,
                subscription_manager=state.subscription_manager,
            )

        return await run_zone_scoped(getattr(state, "zone_registry", None), target_zone, rpc_work)

    def _map_error(self, exc: Exception) -> bytes:
        from nexus.contracts.exceptions import (
            ConflictError,
            InvalidPathError,
            NexusFileNotFoundError,
            NexusPermissionError,
            ValidationError,
        )
        from nexus.contracts.process_types import AgentError, InvalidTransitionError

        if isinstance(exc, PermissionError | NexusPermissionError):
            return _error_payload(RPCErrorCode.PERMISSION_ERROR, str(exc))
        if isinstance(exc, NexusFileNotFoundError):
            return _error_payload(RPCErrorCode.FILE_NOT_FOUND, str(exc))
        if isinstance(exc, InvalidPathError):
            return _error_payload(RPCErrorCode.INVALID_PATH, str(exc))
        if isinstance(exc, ValidationError | ValueError):
            return _error_payload(RPCErrorCode.VALIDATION_ERROR, str(exc))
        if isinstance(exc, ConflictError):
            return _error_payload(
                RPCErrorCode.CONFLICT,
                str(exc),
                {
                    "path": exc.path,
                    "expected_content_id": exc.expected_content_id,
                    "current_content_id": exc.current_content_id,
                },
            )
        if isinstance(exc, InvalidTransitionError):
            return _error_payload(RPCErrorCode.CONFLICT, str(exc))
        if isinstance(exc, AgentError):
            return _error_payload(RPCErrorCode.VALIDATION_ERROR, str(exc))
        logger.exception("VFS gRPC call failed")
        return _error_payload(RPCErrorCode.INTERNAL_ERROR, "Internal server error")

    async def Call(
        self,
        request: vfs_pb2.CallRequest,
        context: grpc.aio.ServicerContext,
    ) -> vfs_pb2.CallResponse:
        client_host = _client_host_from_context(context)
        try:
            params = decode_rpc_message(request.payload) if request.payload else {}
            result = await self._dispatch(
                request.method,
                params,
                request.auth_token,
                client_host=client_host,
            )
            return vfs_pb2.CallResponse(
                payload=encode_rpc_message({"result": result}),
                is_error=False,
            )
        except Exception as exc:
            return vfs_pb2.CallResponse(payload=self._map_error(exc), is_error=True)

    async def Read(
        self,
        request: vfs_pb2.ReadRequest,
        context: grpc.aio.ServicerContext,
    ) -> vfs_pb2.ReadResponse:
        client_host = _client_host_from_context(context)
        try:
            result = await self._dispatch(
                "sys_read",
                {"path": request.path},
                request.auth_token,
                client_host=client_host,
            )
            content = _coerce_bytes(result)
            return vfs_pb2.ReadResponse(content=content, size=len(content), is_error=False)
        except Exception as exc:
            return vfs_pb2.ReadResponse(is_error=True, error_payload=self._map_error(exc))

    async def Write(
        self,
        request: vfs_pb2.WriteRequest,
        context: grpc.aio.ServicerContext,
    ) -> vfs_pb2.WriteResponse:
        client_host = _client_host_from_context(context)
        try:
            params: dict[str, Any] = {"path": request.path, "buf": bytes(request.content)}
            if request.content_id:
                params["if_match"] = request.content_id
            result = await self._dispatch(
                "sys_write",
                params,
                request.auth_token,
                client_host=client_host,
            )
            return vfs_pb2.WriteResponse(
                content_id=str(result.get("content_id", "")) if isinstance(result, dict) else "",
                size=int(result.get("size", len(request.content)))
                if isinstance(result, dict)
                else len(request.content),
                gen=int(result.get("gen", 0)) if isinstance(result, dict) else 0,
                is_error=False,
            )
        except Exception as exc:
            return vfs_pb2.WriteResponse(is_error=True, error_payload=self._map_error(exc))

    async def Delete(
        self,
        request: vfs_pb2.DeleteRequest,
        context: grpc.aio.ServicerContext,
    ) -> vfs_pb2.DeleteResponse:
        client_host = _client_host_from_context(context)
        try:
            await self._dispatch(
                "sys_unlink",
                {"path": request.path, "recursive": request.recursive},
                request.auth_token,
                client_host=client_host,
            )
            return vfs_pb2.DeleteResponse(success=True, is_error=False)
        except Exception as exc:
            return vfs_pb2.DeleteResponse(is_error=True, error_payload=self._map_error(exc))

    async def Ping(
        self,
        request: vfs_pb2.PingRequest,
        context: grpc.aio.ServicerContext,
    ) -> vfs_pb2.PingResponse:
        del context, request
        return vfs_pb2.PingResponse(
            version="nexus",
            zone_id=str(getattr(self._app.state.nexus_fs, "zone_id", "root") or "root"),
            uptime_seconds=int(time.monotonic() - self._started),
        )

    async def BatchRead(
        self,
        request: vfs_pb2.BatchReadRequest,
        context: grpc.aio.ServicerContext,
    ) -> vfs_pb2.BatchReadResponse:
        client_host = _client_host_from_context(context)
        results = []
        for item in request.items:
            try:
                params: dict[str, Any] = {"path": item.path}
                if item.offset:
                    params["offset"] = int(item.offset)
                if item.HasField("length"):
                    params["count"] = int(item.length)
                content = _coerce_bytes(
                    await self._dispatch(
                        "sys_read",
                        params,
                        request.auth_token,
                        client_host=client_host,
                    )
                )
                results.append(vfs_pb2.BatchReadItemResponse(content=content, is_error=False))
            except Exception as exc:
                results.append(
                    vfs_pb2.BatchReadItemResponse(is_error=True, error_payload=self._map_error(exc))
                )
        return vfs_pb2.BatchReadResponse(results=results)


async def startup_vfs_grpc(app: "FastAPI") -> None:
    port_raw = os.environ.get("NEXUS_GRPC_PORT", "").strip()
    if not port_raw or port_raw == "0":
        logger.info("VFS gRPC disabled (NEXUS_GRPC_PORT unset or zero)")
        return

    port = int(port_raw)
    host = "0.0.0.0" if _truthy("NEXUS_GRPC_BIND_ALL") else "127.0.0.1"
    server = grpc.aio.server()
    vfs_pb2_grpc.add_NexusVFSServiceServicer_to_server(VFSGrpcServicer(app), server)
    bound_port = server.add_insecure_port(f"{host}:{port}")
    if bound_port == 0:
        raise RuntimeError(f"Failed to bind VFS gRPC server on {host}:{port}")
    await server.start()
    app.state.grpc_server = server
    logger.info("VFS gRPC server listening on %s:%s", host, bound_port)


async def shutdown_vfs_grpc(app: "FastAPI") -> None:
    server = getattr(app.state, "grpc_server", None)
    if server is None:
        return
    await server.stop(grace=5)
    app.state.grpc_server = None
