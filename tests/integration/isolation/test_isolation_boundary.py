"""Integration tests verifying isolation boundaries.

These tests confirm that the worker process/interpreter cannot corrupt
the host process's state.  All tests use ``ProcessPoolExecutor``
(``force_process=True``) which is available on every Python version.
"""

from __future__ import annotations

import sys

from nexus.isolation import IsolatedBackend, IsolationConfig

# Path to helpers defined in this file (importable by child processes).
_HELPER_MOD = "tests.integration.isolation.test_isolation_boundary"


def _cfg(
    module: str = "tests.unit.isolation.conftest", cls: str = "MockBackend", **kw: object
) -> IsolationConfig:
    defaults: dict = {
        "backend_module": module,
        "backend_class": cls,
        "pool_size": 1,
        "call_timeout": 30.0,
        "startup_timeout": 30.0,
        "force_process": True,
    }
    defaults.update(kw)
    return IsolationConfig(**defaults)


# ── Helper backends for boundary tests (must be top-level for pickle) ──


class SysModulesMutator:
    """Backend that injects a key into sys.modules."""

    @property
    def name(self) -> str:
        return "mutator"

    def connect(self, context=None):
        from nexus.backends.backend import HandlerStatusResponse

        sys.modules["__isolation_test_marker__"] = type(sys)("marker")
        return HandlerStatusResponse(success=True)

    def disconnect(self, context=None) -> None:
        pass

    def check_connection(self, context=None):
        from nexus.backends.backend import HandlerStatusResponse

        present = "__isolation_test_marker__" in sys.modules
        return HandlerStatusResponse(success=present)

    # Stubs for abstract methods (unused in boundary tests)
    def write_content(self, content, context=None):
        from nexus.core.response import HandlerResponse

        return HandlerResponse.ok(data="hash", backend_name=self.name)

    def read_content(self, h, context=None):
        from nexus.core.response import HandlerResponse

        return HandlerResponse.ok(data=b"", backend_name=self.name)

    def delete_content(self, h, context=None):
        from nexus.core.response import HandlerResponse

        return HandlerResponse.ok(data=None, backend_name=self.name)

    def content_exists(self, h, context=None):
        from nexus.core.response import HandlerResponse

        return HandlerResponse.ok(data=False, backend_name=self.name)

    def get_content_size(self, h, context=None):
        from nexus.core.response import HandlerResponse

        return HandlerResponse.ok(data=0, backend_name=self.name)

    def get_ref_count(self, h, context=None):
        from nexus.core.response import HandlerResponse

        return HandlerResponse.ok(data=0, backend_name=self.name)

    def mkdir(self, path, parents=False, exist_ok=False, context=None):
        from nexus.core.response import HandlerResponse

        return HandlerResponse.ok(data=None, backend_name=self.name)

    def rmdir(self, path, recursive=False, context=None):
        from nexus.core.response import HandlerResponse

        return HandlerResponse.ok(data=None, backend_name=self.name)

    def is_directory(self, path, context=None):
        from nexus.core.response import HandlerResponse

        return HandlerResponse.ok(data=False, backend_name=self.name)


class GlobalMutator(SysModulesMutator):
    """Backend that sets a global variable in the worker."""

    _GLOBAL_FLAG = False

    @property
    def name(self) -> str:
        return "global_mutator"

    def connect(self, context=None):
        from nexus.backends.backend import HandlerStatusResponse

        GlobalMutator._GLOBAL_FLAG = True
        return HandlerStatusResponse(success=True)

    def check_connection(self, context=None):
        from nexus.backends.backend import HandlerStatusResponse

        return HandlerStatusResponse(success=GlobalMutator._GLOBAL_FLAG)


class CrashingBackend(SysModulesMutator):
    """Backend whose check_connection raises SystemExit."""

    @property
    def name(self) -> str:
        return "crasher"

    def check_connection(self, context=None):
        raise SystemExit(1)


# ═══════════════════════════════════════════════════════════════════════
# Boundary tests
# ═══════════════════════════════════════════════════════════════════════


class TestSysModulesIsolation:
    def test_worker_mutation_does_not_leak(self) -> None:
        """Backend modifies sys.modules in the worker → host is unchanged."""
        backend = IsolatedBackend(_cfg(_HELPER_MOD, "SysModulesMutator"))
        try:
            status = backend.connect()
            assert status.success is True
            # Worker has the marker
            check = backend._pool.submit("check_connection", (), {"context": None})
            assert check.success is True
            # Host does NOT have the marker
            assert "__isolation_test_marker__" not in sys.modules
        finally:
            backend.disconnect()


class TestGlobalStateIsolation:
    def test_worker_global_does_not_leak(self) -> None:
        """Backend sets a class variable in the worker → host copy is unchanged."""
        backend = IsolatedBackend(_cfg(_HELPER_MOD, "GlobalMutator"))
        try:
            status = backend.connect()
            assert status.success is True
            # Host-side flag should still be False
            assert GlobalMutator._GLOBAL_FLAG is False
        finally:
            backend.disconnect()


class TestCrashContainment:
    def test_system_exit_in_worker_does_not_crash_host(self) -> None:
        """Backend raises SystemExit → IsolationCallError, host continues."""
        backend = IsolatedBackend(_cfg(_HELPER_MOD, "CrashingBackend"))
        try:
            status = backend.connect()
            # SystemExit in worker should be caught and reported as failure
            assert status.success is False
        finally:
            backend.disconnect()


class TestImportFailure:
    def test_nonexistent_module_graceful_error(self) -> None:
        """Bad module path → IsolationStartupError (not host crash)."""
        backend = IsolatedBackend(_cfg("no.such.module.at.all", "FakeClass"))
        status = backend.connect()
        assert status.success is False
        assert "no.such.module.at.all" in (status.error_message or "")
        backend.disconnect()


class TestCrossBrickIsolation:
    def test_two_backends_isolated(self) -> None:
        """Two IsolatedBackends in separate pools cannot see each other's state."""
        b1 = IsolatedBackend(_cfg())
        b2 = IsolatedBackend(_cfg())
        try:
            wr1 = b1.write_content(b"only-in-b1")
            assert wr1.success is True

            # b2's worker has a fresh MockBackend — it should not have b1's data
            exists = b2.content_exists(wr1.data)
            assert exists.data is False
        finally:
            b1.disconnect()
            b2.disconnect()
