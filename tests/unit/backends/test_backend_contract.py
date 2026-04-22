"""Backend contract tests -- parametrized across MockBackend and CASLocalBackend (Issue #1601)."""

import hashlib
from typing import Any

import pytest

from nexus.backends.base.backend import Backend
from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.object_store import WriteResult
from nexus.core.protocols.connector import (
    ConnectorProtocol,
    ContentStoreProtocol,
    DirectoryOpsProtocol,
)


class _MockBackend(Backend):
    """In-memory Backend for contract testing."""

    def __init__(self) -> None:
        self._content: dict[str, bytes] = {}
        self._ref_counts: dict[str, int] = {}
        self._dirs: set[str] = set()

    @property
    def name(self) -> str:
        return "mock"

    def _hash(self, content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def write_content(
        self, content: bytes, content_id: str = "", *, offset: int = 0, context: Any = None
    ) -> WriteResult:
        content_hash = self._hash(content)
        if content_hash in self._content:
            self._ref_counts[content_hash] += 1
        else:
            self._content[content_hash] = content
            self._ref_counts[content_hash] = 1
        return WriteResult(content_id=content_hash, size=len(content))

    def read_content(self, content_hash: str, context: Any = None) -> bytes:
        if content_hash not in self._content:
            raise NexusFileNotFoundError(content_hash)
        return self._content[content_hash]

    def delete_content(self, content_hash: str, context: Any = None) -> None:
        if content_hash not in self._content:
            raise NexusFileNotFoundError(content_hash)
        self._ref_counts[content_hash] -= 1
        if self._ref_counts[content_hash] <= 0:
            del self._content[content_hash]
            del self._ref_counts[content_hash]

    def content_exists(self, content_hash: str, context: Any = None) -> bool:
        return content_hash in self._content

    def get_content_size(self, content_hash: str, context: Any = None) -> int:
        if content_hash not in self._content:
            raise NexusFileNotFoundError(content_hash)
        return len(self._content[content_hash])

    def mkdir(
        self, path: str, parents: bool = False, exist_ok: bool = False, context: Any = None
    ) -> None:
        if path in self._dirs and not exist_ok:
            raise BackendError("Directory exists", backend="mock")
        self._dirs.add(path)
        if parents:
            parts = path.strip("/").split("/")
            for i in range(1, len(parts)):
                self._dirs.add("/" + "/".join(parts[:i]))

    def rmdir(self, path: str, recursive: bool = False, context: Any = None) -> None:
        if path not in self._dirs:
            raise NexusFileNotFoundError(path)
        self._dirs.discard(path)

    def is_directory(self, path: str, context: Any = None) -> bool:
        return path in self._dirs


class _IncompleteBackend:
    """Missing most protocol methods -- should fail isinstance checks."""

    @property
    def name(self) -> str:
        return "incomplete"


@pytest.fixture()
def mock_backend() -> _MockBackend:
    return _MockBackend()


@pytest.fixture()
def local_backend(tmp_path: Any) -> Backend:
    from nexus.backends.storage.cas_local import CASLocalBackend

    return CASLocalBackend(root_path=str(tmp_path / "nexus-data"))


@pytest.fixture(params=["mock", "local"])
def backend(request: Any, tmp_path: Any) -> Backend:
    """Parametrized fixture providing mock and local backends."""
    if request.param == "mock":
        return _MockBackend()
    elif request.param == "local":
        from nexus.backends.storage.cas_local import CASLocalBackend

        return CASLocalBackend(root_path=str(tmp_path / "nexus-data"))
    raise ValueError(f"Unknown backend: {request.param}")


class TestBackendContract:
    """Contract tests run against all backend implementations."""

    # -- Content Operations (CAS) --

    def test_write_then_read_roundtrip(self, backend: Backend) -> None:
        content = b"hello world"
        write_result = backend.write_content(content)
        content_hash = write_result.content_id
        assert content_hash is not None
        data = backend.read_content(content_hash)
        assert data == content

    def test_content_exists_after_write(self, backend: Backend) -> None:
        content = b"test exists"
        content_hash = backend.write_content(content).content_id
        assert content_hash is not None
        assert backend.content_exists(content_hash) is True

    def test_content_not_exists_for_unknown(self, backend: Backend) -> None:
        assert backend.content_exists("0" * 64) is False

    def test_delete_removes_content(self, backend: Backend) -> None:
        content = b"delete me"
        content_hash = backend.write_content(content).content_id
        assert content_hash is not None
        backend.delete_content(content_hash)
        assert backend.content_exists(content_hash) is False

    def test_write_is_idempotent(self, backend: Backend) -> None:
        """CAS: writing same content twice returns same hash."""
        content = b"idempotent data"
        result1 = backend.write_content(content)
        result2 = backend.write_content(content)
        assert result1.content_id is not None
        assert result2.content_id is not None
        assert result1.content_id == result2.content_id

    def test_get_content_size_correct(self, backend: Backend) -> None:
        content = b"measure me"
        content_hash = backend.write_content(content).content_id
        assert content_hash is not None
        size = backend.get_content_size(content_hash)
        assert size == len(content)

    def test_read_nonexistent_content_fails(self, backend: Backend) -> None:
        with pytest.raises((NexusFileNotFoundError, BackendError)):
            backend.read_content("a" * 64)

    def test_delete_nonexistent_is_safe(self, backend: Backend) -> None:
        """Deleting non-existent content should raise."""
        with pytest.raises((NexusFileNotFoundError, BackendError)):
            backend.delete_content("b" * 64)

    def test_batch_read_partial_missing(self, backend: Backend) -> None:
        content = b"batch test"
        real_hash = backend.write_content(content).content_id
        assert real_hash is not None
        fake_hash = "c" * 64
        results = backend.batch_read_content([real_hash, fake_hash])
        assert results[real_hash] == content
        assert results[fake_hash] is None

    # -- Directory Operations --

    def test_mkdir_creates_directory(self, backend: Backend) -> None:
        backend.mkdir("/testdir", exist_ok=True)
        assert backend.is_directory("/testdir") is True

    def test_rmdir_removes_directory(self, backend: Backend) -> None:
        backend.mkdir("/toremove", exist_ok=True)
        backend.rmdir("/toremove")
        assert backend.is_directory("/toremove") is False

    def test_is_directory_false_for_nonexistent(self, backend: Backend) -> None:
        assert backend.is_directory("/nonexistent") is False

    def test_mkdir_parents(self, backend: Backend) -> None:
        backend.mkdir("/a/b/c", parents=True, exist_ok=True)

    def test_mkdir_exist_ok(self, backend: Backend) -> None:
        backend.mkdir("/existing", exist_ok=True)
        backend.mkdir("/existing", exist_ok=True)

    # -- Capability Flags --

    def test_capability_flags_are_booleans(self, backend: Backend) -> None:
        assert isinstance(backend.is_connected, bool)
        assert isinstance(backend.has_root_path, bool)

    def test_name_returns_string(self, backend: Backend) -> None:
        assert isinstance(backend.name, str)
        assert len(backend.name) > 0

    # -- Connection Lifecycle --

    def test_check_connection_returns_status(self, backend: Backend) -> None:
        assert isinstance(backend.check_connection().success, bool)

    # -- Protocol Conformance --

    def test_backend_satisfies_content_store_protocol(self, backend: Backend) -> None:
        assert isinstance(backend, ContentStoreProtocol)

    def test_backend_satisfies_directory_ops_protocol(self, backend: Backend) -> None:
        assert isinstance(backend, DirectoryOpsProtocol)

    def test_backend_satisfies_connector_protocol(self, backend: Backend) -> None:
        assert isinstance(backend, ConnectorProtocol)

    # -- Context Passthrough --

    def test_write_with_context(self, backend: Backend) -> None:
        """Passing context=None should not crash."""
        backend.write_content(b"ctx test", context=None)

    def test_read_with_context(self, backend: Backend) -> None:
        content_hash = backend.write_content(b"ctx read", context=None).content_id
        assert content_hash is not None
        backend.read_content(content_hash, context=None)


class TestCASLocalBackendSpecific:
    """CASLocalBackend-specific tests not covered by contract."""

    def test_local_has_root_path(self, local_backend: Backend) -> None:
        assert local_backend.has_root_path is True


class TestNegativeProtocolConformance:
    """Incomplete objects must NOT satisfy protocols."""

    def test_incomplete_not_content_store(self) -> None:
        obj = _IncompleteBackend()
        assert not isinstance(obj, ContentStoreProtocol)

    def test_incomplete_not_connector(self) -> None:
        obj = _IncompleteBackend()
        assert not isinstance(obj, ConnectorProtocol)

    def test_incomplete_not_directory_ops(self) -> None:
        obj = _IncompleteBackend()
        assert not isinstance(obj, DirectoryOpsProtocol)
