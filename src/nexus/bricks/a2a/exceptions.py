"""A2A protocol exceptions.

Maps A2A-specific error conditions to JSON-RPC error codes
per the A2A specification.  Each exception also carries a
``grpc_status`` attribute for the gRPC transport binding (#1726).
"""

from typing import Any

import grpc


class A2AError(Exception):
    """Base exception for A2A protocol errors."""

    code: int = -32603  # Internal error default
    message: str = "Internal error"
    grpc_status: grpc.StatusCode = grpc.StatusCode.INTERNAL

    def __init__(
        self,
        message: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.__class__.message
        self.data = data
        super().__init__(self.message)

    def to_rpc_error(self) -> dict[str, Any]:
        """Serialize to JSON-RPC error object."""
        error: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.data is not None:
            error["data"] = self.data
        return error


class TaskNotFoundError(A2AError):
    """Referenced task does not exist or is not accessible."""

    code = -32001
    message = "Task not found"
    grpc_status = grpc.StatusCode.NOT_FOUND


class TaskNotCancelableError(A2AError):
    """Task is in a terminal state and cannot be canceled."""

    code = -32002
    message = "Task is not cancelable"
    grpc_status = grpc.StatusCode.FAILED_PRECONDITION


class InvalidStateTransitionError(A2AError):
    """Attempted an invalid task state transition."""

    code = -32003
    message = "Invalid state transition"
    grpc_status = grpc.StatusCode.FAILED_PRECONDITION


class UnsupportedOperationError(A2AError):
    """Requested operation is not supported."""

    code = -32004
    message = "Unsupported operation"
    grpc_status = grpc.StatusCode.UNIMPLEMENTED


class PushNotificationNotSupportedError(A2AError):
    """Push notification operations are not available."""

    code = -32006
    message = "Push notifications not supported"
    grpc_status = grpc.StatusCode.UNIMPLEMENTED


# Standard JSON-RPC errors (used by the A2A router)


class InvalidRequestError(A2AError):
    """Invalid JSON-RPC request."""

    code = -32600
    message = "Invalid request"
    grpc_status = grpc.StatusCode.INVALID_ARGUMENT


class MethodNotFoundError(A2AError):
    """JSON-RPC method not found."""

    code = -32601
    message = "Method not found"
    grpc_status = grpc.StatusCode.UNIMPLEMENTED


class InvalidParamsError(A2AError):
    """Invalid method parameters."""

    code = -32602
    message = "Invalid params"
    grpc_status = grpc.StatusCode.INVALID_ARGUMENT
