"""Unit tests for A2A gRPC error mapping.

Verifies that each A2AError subclass has a consistent grpc_status
attribute mapped to the expected gRPC status code.
"""

import grpc
import pytest

from nexus.a2a.exceptions import (
    A2AError,
    ContentTypeNotSupportedError,
    InternalError,
    InvalidParamsError,
    InvalidRequestError,
    InvalidStateTransitionError,
    MethodNotFoundError,
    PushNotificationNotSupportedError,
    TaskNotCancelableError,
    TaskNotFoundError,
    UnsupportedOperationError,
)

class TestGrpcStatusMapping:
    """Verify each exception has the correct grpc_status attribute."""

    @pytest.mark.parametrize(
        ("exc_class", "expected_status"),
        [
            (A2AError, grpc.StatusCode.INTERNAL),
            (TaskNotFoundError, grpc.StatusCode.NOT_FOUND),
            (TaskNotCancelableError, grpc.StatusCode.FAILED_PRECONDITION),
            (InvalidStateTransitionError, grpc.StatusCode.FAILED_PRECONDITION),
            (UnsupportedOperationError, grpc.StatusCode.UNIMPLEMENTED),
            (ContentTypeNotSupportedError, grpc.StatusCode.INVALID_ARGUMENT),
            (PushNotificationNotSupportedError, grpc.StatusCode.UNIMPLEMENTED),
            (InvalidRequestError, grpc.StatusCode.INVALID_ARGUMENT),
            (MethodNotFoundError, grpc.StatusCode.UNIMPLEMENTED),
            (InvalidParamsError, grpc.StatusCode.INVALID_ARGUMENT),
            (InternalError, grpc.StatusCode.INTERNAL),
        ],
    )
    def test_grpc_status_on_class(
        self, exc_class: type[A2AError], expected_status: grpc.StatusCode
    ) -> None:
        assert exc_class.grpc_status == expected_status

    @pytest.mark.parametrize(
        ("exc_class", "expected_status"),
        [
            (TaskNotFoundError, grpc.StatusCode.NOT_FOUND),
            (TaskNotCancelableError, grpc.StatusCode.FAILED_PRECONDITION),
            (InvalidParamsError, grpc.StatusCode.INVALID_ARGUMENT),
        ],
    )
    def test_grpc_status_on_instance(
        self, exc_class: type[A2AError], expected_status: grpc.StatusCode
    ) -> None:
        exc = exc_class()
        assert exc.grpc_status == expected_status

    def test_all_subclasses_have_grpc_status(self) -> None:
        """Every A2AError subclass must define a grpc_status attribute."""
        for cls in A2AError.__subclasses__():
            assert hasattr(cls, "grpc_status"), f"{cls.__name__} missing grpc_status attribute"
            assert isinstance(cls.grpc_status, grpc.StatusCode), (
                f"{cls.__name__}.grpc_status is not a grpc.StatusCode"
            )

    def test_rpc_error_still_works(self) -> None:
        """Ensure to_rpc_error() still works after adding grpc_status."""
        exc = TaskNotFoundError(data={"taskId": "abc"})
        rpc_err = exc.to_rpc_error()

        assert rpc_err["code"] == -32001
        assert rpc_err["message"] == "Task not found"
        assert rpc_err["data"] == {"taskId": "abc"}
