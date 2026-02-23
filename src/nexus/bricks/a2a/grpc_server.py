"""A2A gRPC transport binding — server implementation (#1726).

Implements the ``A2AService`` gRPC servicer that delegates all business
logic to the existing ``TaskManager``.  The gRPC transport runs on a
separate port and is config-gated (``NEXUS_A2A_GRPC_PORT``).
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

import grpc
import grpc.aio

from nexus.bricks.a2a import a2a_pb2, a2a_pb2_grpc
from nexus.bricks.a2a.exceptions import A2AError
from nexus.bricks.a2a.models import TERMINAL_STATES
from nexus.bricks.a2a.proto_converter import (
    send_request_from_proto,
    task_to_proto,
)
from nexus.contracts.constants import ROOT_ZONE_ID

logger = logging.getLogger(__name__)


class A2AServicer(a2a_pb2_grpc.A2AServiceServicer):
    """gRPC servicer for the A2A protocol.

    Reuses the existing ``TaskManager`` for all business logic so that
    HTTP+JSON-RPC and gRPC transports share the same task state.
    """

    def __init__(self, task_manager: Any, zone_id: str = ROOT_ZONE_ID) -> None:
        self._tm = task_manager
        self._zone_id = zone_id

    async def SendMessage(
        self,
        request: a2a_pb2.SendMessageRequest,
        context: grpc.aio.ServicerContext,
    ) -> a2a_pb2.SendMessageResponse:
        """Create or continue a task with a user message."""
        try:
            msg, metadata = send_request_from_proto(request)
            task = await self._tm.create_task(
                msg,
                zone_id=self._zone_id,
                metadata=metadata,
            )
            return a2a_pb2.SendMessageResponse(task=task_to_proto(task))
        except A2AError as exc:
            await context.abort(exc.grpc_status, exc.message)
        except Exception as exc:
            logger.exception("SendMessage failed: %s", exc)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))

    async def SendStreamingMessage(
        self,
        request: a2a_pb2.SendMessageRequest,
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[a2a_pb2.StreamResponse]:
        """Create or continue a task, streaming status/artifact updates."""
        try:
            msg, metadata = send_request_from_proto(request)
            task = await self._tm.create_task(
                msg,
                zone_id=self._zone_id,
                metadata=metadata,
            )
        except A2AError as exc:
            await context.abort(exc.grpc_status, exc.message)
            return
        except Exception as exc:
            logger.exception("SendStreamingMessage failed: %s", exc)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))
            return

        # Yield the initial task
        yield a2a_pb2.StreamResponse(task=task_to_proto(task))

        # Stream subsequent events
        queue = self._tm.register_stream(task.id)
        try:
            async for response in self._stream_events(task.id, queue, context):
                yield response
        finally:
            self._tm.unregister_stream(task.id, queue)

    async def GetTask(
        self,
        request: a2a_pb2.GetTaskRequest,
        context: grpc.aio.ServicerContext,
    ) -> a2a_pb2.Task:
        """Retrieve a task by ID."""
        try:
            history_length = request.history_length if request.history_length else None
            task = await self._tm.get_task(
                request.id,
                zone_id=self._zone_id,
                history_length=history_length,
            )
            return task_to_proto(task)
        except A2AError as exc:
            await context.abort(exc.grpc_status, exc.message)
        except Exception as exc:
            logger.exception("GetTask failed: %s", exc)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))

    async def CancelTask(
        self,
        request: a2a_pb2.CancelTaskRequest,
        context: grpc.aio.ServicerContext,
    ) -> a2a_pb2.Task:
        """Cancel a task."""
        try:
            task = await self._tm.cancel_task(request.id, zone_id=self._zone_id)
            return task_to_proto(task)
        except A2AError as exc:
            await context.abort(exc.grpc_status, exc.message)
        except Exception as exc:
            logger.exception("CancelTask failed: %s", exc)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))

    async def SubscribeToTask(
        self,
        request: a2a_pb2.SubscribeToTaskRequest,
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[a2a_pb2.StreamResponse]:
        """Subscribe to updates for an existing task."""
        try:
            task = await self._tm.get_task(request.id, zone_id=self._zone_id)
        except A2AError as exc:
            await context.abort(exc.grpc_status, exc.message)
            return
        except Exception as exc:
            logger.exception("SubscribeToTask failed: %s", exc)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))
            return

        # Yield current task state
        yield a2a_pb2.StreamResponse(task=task_to_proto(task))

        # If already terminal, nothing more to stream
        if task.status.state in TERMINAL_STATES:
            return

        # Stream subsequent events
        queue = self._tm.register_stream(task.id)
        try:
            async for response in self._stream_events(task.id, queue, context):
                yield response
        finally:
            self._tm.unregister_stream(task.id, queue)

    # ------------------------------------------------------------------
    # Streaming helper
    # ------------------------------------------------------------------

    async def _stream_events(
        self,
        _task_id: str,
        queue: Any,
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[a2a_pb2.StreamResponse]:
        """Consume events from a StreamRegistry queue and yield StreamResponses."""
        from nexus.bricks.a2a.proto_converter import (
            artifact_to_proto,
            task_status_to_proto,
        )

        while not context.cancelled():
            event: dict[str, Any] | None = await queue.get()
            if event is None:
                # Sentinel: stream closed
                break

            if "statusUpdate" in event:
                from nexus.bricks.a2a.models import TaskStatusUpdateEvent

                su = TaskStatusUpdateEvent(**event["statusUpdate"])
                pb_status = task_status_to_proto(su.status)
                yield a2a_pb2.StreamResponse(
                    status_update=a2a_pb2.TaskStatusUpdateEvent(
                        task_id=su.taskId,
                        status=pb_status,
                        final=su.final,
                    )
                )
                if su.final:
                    break

            elif "artifactUpdate" in event:
                from nexus.bricks.a2a.models import TaskArtifactUpdateEvent

                au = TaskArtifactUpdateEvent(**event["artifactUpdate"])
                yield a2a_pb2.StreamResponse(
                    artifact_update=a2a_pb2.TaskArtifactUpdateEvent(
                        task_id=au.taskId,
                        artifact=artifact_to_proto(au.artifact),
                        append=au.append or False,
                    )
                )


# ============================================================================
# Server lifecycle
# ============================================================================


async def create_grpc_server(
    task_manager: Any,
    port: int = 2027,
    *,
    tls_cert_path: str | None = None,
    tls_key_path: str | None = None,
    tls_ca_path: str | None = None,
    zone_id: str = ROOT_ZONE_ID,
) -> grpc.aio.Server:
    """Create and configure a gRPC server for the A2A service.

    Parameters
    ----------
    task_manager:
        The ``TaskManager`` instance (shared with HTTP transport).
    port:
        Port to listen on.
    tls_cert_path:
        Path to TLS certificate file (optional).
    tls_key_path:
        Path to TLS private key file (optional).
    tls_ca_path:
        Path to CA certificate for mutual TLS (optional).
    zone_id:
        Default zone ID for task operations.
    """
    server = grpc.aio.server()
    servicer = A2AServicer(task_manager, zone_id=zone_id)
    a2a_pb2_grpc.add_A2AServiceServicer_to_server(servicer, server)

    if (tls_cert_path or tls_key_path) and not (tls_cert_path and tls_key_path):
        raise ValueError("Both tls_cert_path and tls_key_path are required for TLS")

    if tls_cert_path and tls_key_path:
        with open(tls_cert_path, "rb") as f:
            cert = f.read()
        with open(tls_key_path, "rb") as f:
            key = f.read()
        ca_cert = None
        if tls_ca_path:
            with open(tls_ca_path, "rb") as f:
                ca_cert = f.read()
        creds = grpc.ssl_server_credentials(
            [(key, cert)],
            root_certificates=ca_cert,
            require_client_auth=ca_cert is not None,
        )
        server.add_secure_port(f"[::]:{port}", creds)
        logger.info("A2A gRPC server configured with TLS on port %d", port)
    else:
        server.add_insecure_port(f"[::]:{port}")
        logger.info("A2A gRPC server configured (insecure) on port %d", port)

    return server


async def start_grpc_server(server: grpc.aio.Server) -> None:
    """Start the gRPC server."""
    await server.start()
    logger.info("A2A gRPC server started")


async def stop_grpc_server(server: grpc.aio.Server, grace: float = 5.0) -> None:
    """Gracefully stop the gRPC server."""
    await server.stop(grace)
    logger.info("A2A gRPC server stopped")
