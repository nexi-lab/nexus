"""Unit tests for _worker — worker-level functions."""

from __future__ import annotations

import pytest

from nexus.isolation import _worker
from nexus.isolation._worker import (
    worker_call,
    worker_get_property,
    worker_shutdown,
)

# Module-path constants for the MockBackend living in conftest.
_MOD = "tests.unit.isolation.conftest"
_CLS = "MockBackend"
_KW: dict = {}


@pytest.fixture(autouse=True)
def _reset_worker_state() -> None:  # noqa: PT004
    """Ensure each test starts with a clean worker slate."""
    worker_shutdown()
    yield  # type: ignore[misc]
    worker_shutdown()


class TestWorkerCall:
    def test_lazy_init_and_call(self) -> None:
        """First call lazily creates the backend and executes the method."""
        resp = worker_call(_MOD, _CLS, _KW, "write_content", (b"hello",), {})
        assert resp.success is True
        assert isinstance(resp.data, str)

    def test_reuses_instance(self) -> None:
        """Subsequent calls with the same spec reuse the backend instance."""
        worker_call(_MOD, _CLS, _KW, "write_content", (b"data",), {})
        inst1 = _worker._BACKEND_INSTANCE

        worker_call(_MOD, _CLS, _KW, "content_exists", ("abc",), {})
        inst2 = _worker._BACKEND_INSTANCE

        assert inst1 is inst2

    def test_spec_change_replaces_instance(self) -> None:
        """If the spec changes, the old backend is replaced."""
        worker_call(_MOD, _CLS, _KW, "write_content", (b"data",), {})
        inst1 = _worker._BACKEND_INSTANCE

        # Different kwargs → different spec
        worker_call(_MOD, _CLS, {"fail_connect": False}, "write_content", (b"data",), {})
        inst2 = _worker._BACKEND_INSTANCE

        assert inst1 is not inst2

    def test_roundtrip_write_read(self) -> None:
        """Write → read roundtrip through the worker."""
        wr = worker_call(_MOD, _CLS, _KW, "write_content", (b"roundtrip",), {})
        content_hash = wr.data

        rd = worker_call(_MOD, _CLS, _KW, "read_content", (content_hash,), {})
        assert rd.success is True
        assert rd.data == b"roundtrip"

    def test_exception_propagation(self) -> None:
        """If the backend method raises, the exception propagates."""
        with pytest.raises(FileNotFoundError):
            worker_call(_MOD, _CLS, _KW, "list_dir", ("/nonexistent",), {})

    def test_bad_module_raises(self) -> None:
        with pytest.raises(ModuleNotFoundError):
            worker_call("no.such.module", "Cls", {}, "name", (), {})

    def test_bad_class_raises(self) -> None:
        with pytest.raises(AttributeError):
            worker_call(_MOD, "NoSuchClass", {}, "name", (), {})


class TestWorkerGetProperty:
    def test_name(self) -> None:
        name = worker_get_property(_MOD, _CLS, _KW, "name")
        assert name == "mock"

    def test_user_scoped(self) -> None:
        assert worker_get_property(_MOD, _CLS, _KW, "user_scoped") is False

    def test_thread_safe(self) -> None:
        assert worker_get_property(_MOD, _CLS, _KW, "thread_safe") is True


class TestWorkerConnectFailure:
    """Fix #1: connect() failure must NOT leave corrupted global state."""

    def test_connect_failure_does_not_corrupt_state(self) -> None:
        """If connect() raises, worker globals remain clean for retry."""
        with pytest.raises(ConnectionError):
            worker_call(
                _MOD,
                "FailConnectBackend",
                {},
                "write_content",
                (b"data",),
                {},
            )
        # Globals should be clean — no corrupted half-init instance
        assert _worker._BACKEND_INSTANCE is None
        assert _worker._BACKEND_SPEC is None

    def test_recovery_after_connect_failure(self) -> None:
        """After a connect failure, the next call with a good backend succeeds."""
        with pytest.raises(ConnectionError):
            worker_call(
                _MOD,
                "FailConnectBackend",
                {},
                "write_content",
                (b"data",),
                {},
            )
        # Now call with the working backend
        resp = worker_call(_MOD, _CLS, _KW, "write_content", (b"recovered",), {})
        assert resp.success is True


class TestWorkerSpecOrdering:
    """Fix #2: dict key ordering must not cause unnecessary instance recreation."""

    def test_sorted_spec_prevents_unnecessary_recreation(self) -> None:
        """Spec uses sorted kwargs so insertion order does not matter."""
        from nexus.isolation._worker import _ensure_backend

        # Use the internal function to verify spec comparison directly.
        # MockBackend only accepts fail_connect, so we test spec logic
        # by verifying sorted tuple equality.
        _ensure_backend(_MOD, _CLS, {"fail_connect": False})
        inst1 = _worker._BACKEND_INSTANCE
        spec1 = _worker._BACKEND_SPEC

        # Same logical kwargs — instance should be reused
        _ensure_backend(_MOD, _CLS, {"fail_connect": False})
        inst2 = _worker._BACKEND_INSTANCE

        assert inst1 is inst2
        # Verify the spec uses sorted items
        assert spec1 == (_MOD, _CLS, (("fail_connect", False),))


class TestWorkerShutdown:
    def test_shutdown_clears_instance(self) -> None:
        worker_call(_MOD, _CLS, _KW, "write_content", (b"x",), {})
        assert _worker._BACKEND_INSTANCE is not None

        worker_shutdown()
        assert _worker._BACKEND_INSTANCE is None
        assert _worker._BACKEND_SPEC is None

    def test_shutdown_idempotent(self) -> None:
        worker_shutdown()
        worker_shutdown()  # should not raise
