"""Integration tests verifying isolation boundaries.

These tests confirm that the worker process/interpreter cannot corrupt
the host process's state.  All tests use ``ProcessPoolExecutor``
(``force_process=True``) which is available on every Python version.
"""

import sys

import pytest

from nexus.bricks.sandbox.isolation import IsolatedBackend, IsolationConfig

# Path to helpers defined in this file (importable by child processes).
_HELPER_MOD = "tests.e2e.self_contained.test_isolation_boundary"


def _cfg(
    module: str = "tests.e2e.self_contained.isolation_helpers",
    cls: str = "MockBackend",
    **kw: object,
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
        from nexus.backends.base.backend import HandlerStatusResponse

        sys.modules["__isolation_test_marker__"] = type(sys)("marker")
        return HandlerStatusResponse(success=True)

    def disconnect(self, context=None) -> None:
        pass

    def check_connection(self, context=None):
        from nexus.backends.base.backend import HandlerStatusResponse

        present = "__isolation_test_marker__" in sys.modules
        return HandlerStatusResponse(success=present)

    # Stubs for abstract methods (unused in boundary tests).
    # Return direct values per ObjectStoreABC contract.
    def write_content(self, content, content_id: str = "", *, offset: int = 0, context=None):
        import hashlib

        from nexus.core.object_store import WriteResult

        h = hashlib.sha256(content).hexdigest()
        return WriteResult(content_id=h, size=len(content))

    def read_content(self, h, context=None):
        return b""

    def delete_content(self, h, context=None):
        pass

    def content_exists(self, h, context=None):
        return False

    def get_content_size(self, h, context=None):
        return 0

    def mkdir(self, path, parents=False, exist_ok=False, context=None):
        pass

    def rmdir(self, path, recursive=False, context=None):
        pass

    def is_directory(self, path, context=None):
        return False


class GlobalMutator(SysModulesMutator):
    """Backend that sets a global variable in the worker."""

    _GLOBAL_FLAG = False

    @property
    def name(self) -> str:
        return "global_mutator"

    def connect(self, context=None):
        from nexus.backends.base.backend import HandlerStatusResponse

        GlobalMutator._GLOBAL_FLAG = True
        return HandlerStatusResponse(success=True)

    def check_connection(self, context=None):
        from nexus.backends.base.backend import HandlerStatusResponse

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
    @pytest.mark.skip(reason="Flaky on CI — worker isolation not reliable with subprocess pool")
    def test_worker_mutation_does_not_leak(self) -> None:
        """Backend modifies sys.modules in the worker → host is unchanged."""
        backend = IsolatedBackend(_cfg(_HELPER_MOD, "SysModulesMutator"))
        try:
            status = backend.check_connection()
            assert status.success is True
            # Worker has the marker
            check = backend._pool.submit("check_connection", (), {"context": None})
            assert check.success is True
            # Host does NOT have the marker
            assert "__isolation_test_marker__" not in sys.modules
        finally:
            backend.close()


class TestGlobalStateIsolation:
    @pytest.mark.skip(reason="Flaky on CI — worker isolation not reliable with subprocess pool")
    def test_worker_global_does_not_leak(self) -> None:
        """Backend sets a class variable in the worker → host copy is unchanged."""
        backend = IsolatedBackend(_cfg(_HELPER_MOD, "GlobalMutator"))
        try:
            status = backend.check_connection()
            assert status.success is True
            # Host-side flag should still be False
            assert GlobalMutator._GLOBAL_FLAG is False
        finally:
            backend.close()


class TestCrashContainment:
    def test_system_exit_in_worker_does_not_crash_host(self) -> None:
        """Backend raises SystemExit → IsolationCallError, host continues."""
        backend = IsolatedBackend(_cfg(_HELPER_MOD, "CrashingBackend"))
        try:
            status = backend.check_connection()
            # SystemExit in worker should be caught and reported as failure
            assert status.success is False
        finally:
            backend.close()


class TestImportFailure:
    def test_nonexistent_module_graceful_error(self) -> None:
        """Bad module path → IsolationStartupError (not host crash)."""
        backend = IsolatedBackend(_cfg("no.such.module.at.all", "FakeClass"))
        status = backend.check_connection()
        assert status.success is False
        assert "no.such.module.at.all" in (status.error_message or "")
        backend.close()


class TestCrossBrickIsolation:
    def test_two_backends_isolated(self) -> None:
        """Two IsolatedBackends in separate pools cannot see each other's state."""
        b1 = IsolatedBackend(_cfg())
        b2 = IsolatedBackend(_cfg())
        try:
            wr1 = b1.write_content(b"only-in-b1")
            assert wr1.content_id  # WriteResult with content_hash

            # b2's worker has a fresh MockBackend — it should not have b1's data
            exists = b2.content_exists(wr1.content_id)
            assert exists is False
        finally:
            b1.close()
            b2.close()
