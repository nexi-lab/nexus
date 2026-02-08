"""Unit tests for A2A protocol exceptions."""

from __future__ import annotations

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


class TestA2AErrorBase:
    def test_default_message(self) -> None:
        err = A2AError()
        assert err.message == "Internal error"
        assert err.code == -32603

    def test_custom_message(self) -> None:
        err = A2AError(message="custom message")
        assert err.message == "custom message"

    def test_with_data(self) -> None:
        err = A2AError(data={"key": "value"})
        assert err.data == {"key": "value"}

    def test_to_rpc_error_basic(self) -> None:
        err = A2AError(message="test")
        rpc = err.to_rpc_error()
        assert rpc["code"] == -32603
        assert rpc["message"] == "test"
        assert "data" not in rpc

    def test_to_rpc_error_with_data(self) -> None:
        err = A2AError(message="test", data={"detail": "more info"})
        rpc = err.to_rpc_error()
        assert rpc["data"] == {"detail": "more info"}

    def test_is_exception(self) -> None:
        err = A2AError()
        assert isinstance(err, Exception)

    def test_str(self) -> None:
        err = A2AError(message="fail")
        assert str(err) == "fail"


class TestTaskNotFoundError:
    def test_code(self) -> None:
        err = TaskNotFoundError()
        assert err.code == -32001
        assert err.message == "Task not found"

    def test_with_data(self) -> None:
        err = TaskNotFoundError(data={"taskId": "abc"})
        rpc = err.to_rpc_error()
        assert rpc["code"] == -32001
        assert rpc["data"]["taskId"] == "abc"


class TestTaskNotCancelableError:
    def test_code(self) -> None:
        err = TaskNotCancelableError()
        assert err.code == -32002


class TestInvalidStateTransitionError:
    def test_code(self) -> None:
        err = InvalidStateTransitionError()
        assert err.code == -32003

    def test_custom_message(self) -> None:
        err = InvalidStateTransitionError(
            message="Cannot go from completed to working",
            data={"currentState": "completed", "requestedState": "working"},
        )
        assert "completed" in err.message
        assert err.data["currentState"] == "completed"


class TestUnsupportedOperationError:
    def test_code(self) -> None:
        err = UnsupportedOperationError()
        assert err.code == -32004


class TestContentTypeNotSupportedError:
    def test_code(self) -> None:
        err = ContentTypeNotSupportedError()
        assert err.code == -32005


class TestPushNotificationNotSupportedError:
    def test_code(self) -> None:
        err = PushNotificationNotSupportedError()
        assert err.code == -32006


class TestInvalidRequestError:
    def test_code(self) -> None:
        err = InvalidRequestError()
        assert err.code == -32600


class TestMethodNotFoundError:
    def test_code(self) -> None:
        err = MethodNotFoundError()
        assert err.code == -32601


class TestInvalidParamsError:
    def test_code(self) -> None:
        err = InvalidParamsError()
        assert err.code == -32602


class TestInternalError:
    def test_code(self) -> None:
        err = InternalError()
        assert err.code == -32603


class TestInheritance:
    """All A2A errors inherit from A2AError."""

    @pytest.mark.parametrize(
        "cls",
        [
            TaskNotFoundError,
            TaskNotCancelableError,
            InvalidStateTransitionError,
            UnsupportedOperationError,
            ContentTypeNotSupportedError,
            PushNotificationNotSupportedError,
            InvalidRequestError,
            MethodNotFoundError,
            InvalidParamsError,
            InternalError,
        ],
    )
    def test_inherits_from_a2a_error(self, cls: type) -> None:
        err = cls()
        assert isinstance(err, A2AError)
        assert isinstance(err, Exception)


class TestErrorCodesUnique:
    """All A2A-specific error codes (non-standard) are unique."""

    def test_no_duplicate_codes(self) -> None:
        a2a_errors = [
            TaskNotFoundError,
            TaskNotCancelableError,
            InvalidStateTransitionError,
            UnsupportedOperationError,
            ContentTypeNotSupportedError,
            PushNotificationNotSupportedError,
        ]
        codes = [cls.code for cls in a2a_errors]
        assert len(codes) == len(set(codes)), f"Duplicate codes: {codes}"
