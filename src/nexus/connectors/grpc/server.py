"""ConnectorGrpcServer — wraps any ObjectStoreABC as a gRPC service.

Rust kernel's GrpcObjectStoreAdapter connects to this server, making
gRPC calls instead of crossing GIL via PyObjectStoreAdapter.

Usage:
    from nexus.connectors.grpc.server import ConnectorGrpcServer

    server = ConnectorGrpcServer(my_backend, port=50051)
    server.start()   # non-blocking
    server.stop()    # graceful shutdown

The server runs in-process (separate thread). For production, can also
run as a sidecar process.

Issue #1868: Phase 11 — Direction 3 gRPC adapter.
"""

from __future__ import annotations

import importlib
import logging
from concurrent import futures
from typing import TYPE_CHECKING, Any

import grpc

if TYPE_CHECKING:
    from nexus.contracts.storage import ObjectStoreABC

logger = logging.getLogger(__name__)

# Import generated proto modules dynamically (no type stubs available)
_pb2 = importlib.import_module("nexus.connectors.grpc.nexus.storage.object_store_pb2")
_pb2_grpc = importlib.import_module("nexus.connectors.grpc.nexus.storage.object_store_pb2_grpc")


class _ObjectStoreServicer:
    """gRPC servicer that delegates to a Python ObjectStoreABC backend."""

    def __init__(self, backend: ObjectStoreABC) -> None:
        self._backend = backend

    def ReadContent(self, request: Any, context: grpc.ServicerContext) -> Any:
        try:
            data = self._backend.read_content(
                request.content_id,
                context=self._make_ctx(request),
            )
            return _pb2.ReadContentResponse(data=data)
        except FileNotFoundError:
            context.abort(grpc.StatusCode.NOT_FOUND, f"Not found: {request.backend_path}")
        except Exception as e:
            context.abort(grpc.StatusCode.INTERNAL, str(e))
        return _pb2.ReadContentResponse()

    def WriteContent(self, request: Any, context: grpc.ServicerContext) -> Any:
        try:
            result = self._backend.write_content(
                request.content,
                request.content_id,
                context=self._make_ctx(request),
            )
            return _pb2.WriteContentResponse(
                content_id=result.content_id,
                version=getattr(result, "version", result.content_id),
                size=getattr(result, "size", len(request.content)),
            )
        except Exception as e:
            context.abort(grpc.StatusCode.INTERNAL, str(e))
        return _pb2.WriteContentResponse()

    def DeleteFile(self, request: Any, context: grpc.ServicerContext) -> Any:
        try:
            if hasattr(self._backend, "delete"):
                self._backend.delete(request.path)
            elif hasattr(self._backend, "delete_file"):
                self._backend.delete_file(request.path)
        except Exception as e:
            context.abort(grpc.StatusCode.INTERNAL, str(e))
        return _pb2.DeleteFileResponse()

    def Mkdir(self, request: Any, context: grpc.ServicerContext) -> Any:
        try:
            self._backend.mkdir(request.path, parents=request.parents, exist_ok=request.exist_ok)
        except Exception as e:
            context.abort(grpc.StatusCode.INTERNAL, str(e))
        return _pb2.MkdirResponse()

    def Rmdir(self, request: Any, context: grpc.ServicerContext) -> Any:
        try:
            self._backend.rmdir(request.path, recursive=request.recursive)
        except Exception as e:
            context.abort(grpc.StatusCode.INTERNAL, str(e))
        return _pb2.RmdirResponse()

    def Rename(self, request: Any, context: grpc.ServicerContext) -> Any:
        try:
            self._backend.rename(request.old_path, request.new_path)
        except Exception as e:
            context.abort(grpc.StatusCode.INTERNAL, str(e))
        return _pb2.RenameResponse()

    def _make_ctx(self, request: Any) -> Any:
        """Build a minimal OperationContext from gRPC request fields."""
        try:
            from nexus.contracts.types import OperationContext

            return OperationContext(
                user_id=getattr(request, "user_id", "anonymous") or "anonymous",
                zone_id=getattr(request, "zone_id", "root") or "root",
                is_admin=getattr(request, "is_admin", False),
                groups=[],
            )
        except (ImportError, AttributeError, TypeError):
            return None


class ConnectorGrpcServer:
    """Wraps an ObjectStoreABC backend as a gRPC service.

    Rust kernel connects to this server via GrpcObjectStoreAdapter,
    eliminating GIL contention during remote I/O (GDrive, Gmail, etc.).
    """

    def __init__(
        self,
        backend: ObjectStoreABC,
        port: int = 50051,
        max_workers: int = 4,
    ) -> None:
        self._backend = backend
        self._port = port
        self._server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
        servicer = _ObjectStoreServicer(backend)
        _pb2_grpc.add_ObjectStoreServiceServicer_to_server(servicer, self._server)
        self._server.add_insecure_port(f"[::]:{port}")

    @property
    def addr(self) -> str:
        """gRPC address for Rust kernel add_mount(grpc_addr=...)."""
        return f"http://127.0.0.1:{self._port}"

    def start(self) -> None:
        """Start the gRPC server (non-blocking)."""
        self._server.start()
        logger.info(
            "ConnectorGrpcServer started on port %d for backend %s",
            self._port,
            self._backend.name,
        )

    def stop(self, grace: float = 1.0) -> None:
        """Graceful shutdown."""
        self._server.stop(grace)
        logger.info("ConnectorGrpcServer stopped")
