"""Unit tests for IsolatedPool."""

from __future__ import annotations

import pytest

from nexus.isolation._pool import IsolatedPool
from nexus.isolation.config import IsolationConfig
from nexus.isolation.errors import (
    IsolationCallError,
    IsolationPoolError,
    IsolationStartupError,
)

# conftest.MockBackend path
_MOD = "tests.unit.isolation.conftest"
_CLS = "MockBackend"


def _cfg(**overrides: object) -> IsolationConfig:
    defaults = {
        "backend_module": _MOD,
        "backend_class": _CLS,
        "pool_size": 1,
        "call_timeout": 30.0,  # generous for ProcessPool cold-start on macOS (spawn)
        "startup_timeout": 30.0,
        "force_process": True,  # always use ProcessPool in tests (predictable)
    }
    defaults.update(overrides)
    return IsolationConfig(**defaults)  # type: ignore[arg-type]


@pytest.fixture()
def pool() -> IsolatedPool:
    p = IsolatedPool(_cfg())
    yield p  # type: ignore[misc]
    p.shutdown()


class TestIsolatedPoolSubmit:
    def test_basic_submit(self, pool: IsolatedPool) -> None:
        resp = pool.submit("write_content", (b"hello",), {})
        assert resp.success is True
        assert isinstance(resp.data, str)

    def test_roundtrip(self, pool: IsolatedPool) -> None:
        wr = pool.submit("write_content", (b"roundtrip",), {})
        rd = pool.submit("read_content", (wr.data,), {})
        assert rd.data == b"roundtrip"

    def test_exception_wrapped_as_call_error(self, pool: IsolatedPool) -> None:
        with pytest.raises(IsolationCallError, match="list_dir"):
            pool.submit("list_dir", ("/no-such-dir",), {})


class TestIsolatedPoolGetProperty:
    def test_name(self, pool: IsolatedPool) -> None:
        assert pool.get_property("name") == "mock"

    def test_user_scoped(self, pool: IsolatedPool) -> None:
        assert pool.get_property("user_scoped") is False


class TestIsolatedPoolStartupError:
    def test_bad_module(self) -> None:
        p = IsolatedPool(_cfg(backend_module="no.such.module"))
        try:
            with pytest.raises(IsolationStartupError, match="no.such.module"):
                p.submit("write_content", (b"x",), {})
        finally:
            p.shutdown()

    def test_bad_class(self) -> None:
        p = IsolatedPool(_cfg(backend_class="NoSuchClass"))
        try:
            with pytest.raises(IsolationStartupError, match="NoSuchClass"):
                p.submit("write_content", (b"x",), {})
        finally:
            p.shutdown()


class TestIsolatedPoolShutdown:
    def test_submit_after_shutdown_raises(self) -> None:
        p = IsolatedPool(_cfg())
        p.shutdown()
        with pytest.raises(IsolationPoolError, match="shut down"):
            p.submit("write_content", (b"x",), {})

    def test_shutdown_idempotent(self) -> None:
        p = IsolatedPool(_cfg())
        p.shutdown()
        p.shutdown()  # should not raise

    def test_is_alive(self) -> None:
        p = IsolatedPool(_cfg())
        assert p.is_alive is True
        p.shutdown()
        assert p.is_alive is False


class TestIsolatedPoolAutoRestart:
    def test_restart_after_max_failures(self) -> None:
        """Pool automatically restarts after max_consecutive_failures."""
        p = IsolatedPool(
            _cfg(
                backend_module="no.such.module",
                max_consecutive_failures=3,
            )
        )
        try:
            for _ in range(3):
                with pytest.raises(IsolationStartupError):
                    p.submit("write_content", (b"x",), {})
            # After 3 failures the pool should have restarted (counter reset)
            assert p._consecutive_failures == 0
        finally:
            p.shutdown()
