"""Unit tests for isolation error hierarchy."""

from __future__ import annotations

import pytest

from nexus.isolation.errors import (
    IsolationCallError,
    IsolationError,
    IsolationPoolError,
    IsolationStartupError,
    IsolationTimeoutError,
)


class TestIsolationErrorHierarchy:
    """All custom errors are subclasses of IsolationError."""

    @pytest.mark.parametrize(
        "exc_cls",
        [IsolationStartupError, IsolationCallError, IsolationTimeoutError, IsolationPoolError],
    )
    def test_subclass_of_base(self, exc_cls: type) -> None:
        assert issubclass(exc_cls, IsolationError)
        assert issubclass(exc_cls, Exception)

    def test_base_is_not_subclass_of_children(self) -> None:
        assert not issubclass(IsolationError, IsolationStartupError)

    def test_catch_all_with_base(self) -> None:
        with pytest.raises(IsolationError):
            raise IsolationCallError("method", cause=ValueError("bad"))


class TestIsolationStartupError:
    def test_message_without_cause(self) -> None:
        exc = IsolationStartupError("my.mod", "MyClass")
        assert "my.mod:MyClass" in str(exc)
        assert exc.cause is None

    def test_message_with_cause(self) -> None:
        cause = ImportError("no module named 'foo'")
        exc = IsolationStartupError("foo", "Bar", cause=cause)
        assert "foo:Bar" in str(exc)
        assert "no module named 'foo'" in str(exc)
        assert exc.cause is cause
        assert exc.module == "foo"
        assert exc.cls == "Bar"


class TestIsolationCallError:
    def test_message_without_cause(self) -> None:
        exc = IsolationCallError("read_content")
        assert "read_content" in str(exc)
        assert exc.cause is None

    def test_message_with_cause(self) -> None:
        cause = FileNotFoundError("missing")
        exc = IsolationCallError("read_content", cause=cause)
        assert "read_content" in str(exc)
        assert "missing" in str(exc)
        assert exc.cause is cause
        assert exc.method == "read_content"


class TestIsolationTimeoutError:
    def test_message(self) -> None:
        exc = IsolationTimeoutError("write_content", 30.0)
        assert "write_content" in str(exc)
        assert "30.0s" in str(exc)
        assert exc.method == "write_content"
        assert exc.timeout == 30.0


class TestIsolationPoolError:
    def test_message(self) -> None:
        exc = IsolationPoolError("pool is shut down")
        assert "pool is shut down" in str(exc)
        assert exc.reason == "pool is shut down"
