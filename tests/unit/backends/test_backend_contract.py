"""Backend contract tests (Issue #1601).

Defines the behavioral contract that ALL Backend implementations must satisfy.
Uses the MockBackend as a reference implementation and runs the same tests
against LocalBackend (via tmp_path) for real-world validation.

Tests verify:
- Content operations (CAS semantics)
- Directory operations
- Capability flags
- Protocol conformance (ContentStoreProtocol, ConnectorProtocol)
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from nexus.backends.backend import Backend
from nexus.core.protocols.connector import (
    ConnectorProtocol,
    ContentStoreProtocol,
    DirectoryOpsProtocol,
)
from nexus.core.response import HandlerResponse

# ---------------------------------------------------------------------------
# Reusable MockBackend (copied from tests/unit/cache/mock_backend.py to
# avoid import issues with conftest)
# ---------------------------------------------------------------------------

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

    def write_content(self, content: bytes, context: Any = None) -> HandlerResponse[str]:
        content_hash = self._hash(content)
        if content_hash in self._content:
            self._ref_counts[content_hash] += 1
        else:
            self._content[content_hash] = content
            self._ref_counts[content_hash] = 1
        return HandlerResponse.ok(data=content_hash, backend_name="mock")

    def read_content(self, content_hash: str, context: Any = None) -> HandlerResponse[bytes]:
        if content_hash not in self._content:
            return HandlerResponse.not_found(
                path=content_hash, message="Content not found", backend_name="mock"
            )
        return HandlerResponse.ok(data=self._content[content_hash], backend_name="mock")

    def delete_content(self, content_hash: str, context: Any = None) -> HandlerResponse[None]:
        if content_hash not in self._content:
            return HandlerResponse.not_found(
                path=content_hash, message="Content not found", backend_name="mock"
            )
        self._ref_counts[content_hash] -= 1
        if self._ref_counts[content_hash] <= 0:
            del self._content[content_hash]
            del self._ref_counts[content_hash]
        return HandlerResponse.ok(data=None, backend_name="mock")

    def content_exists(self, content_hash: str, context: Any = None) -> HandlerResponse[bool]:
        return HandlerResponse.ok(data=content_hash in self._content, backend_name="mock")

    def get_content_size(self, content_hash: str, context: Any = None) -> HandlerResponse[int]:
        if content_hash not in self._content:
            return HandlerResponse.not_found(
                path=content_hash, message="Content not found", backend_name="mock"
            )
        return HandlerResponse.ok(data=len(self._content[content_hash]), backend_name="mock")

    def get_ref_count(self, content_hash: str, context: Any = None) -> HandlerResponse[int]:
        return HandlerResponse.ok(data=self._ref_counts.get(content_hash, 0), backend_name="mock")

    def mkdir(
        self, path: str, parents: bool = False, exist_ok: bool = False, context: Any = None
    ) -> HandlerResponse[None]:
        if path in self._dirs and not exist_ok:
            return HandlerResponse.error("Directory exists", code=409, backend_name="mock")
        self._dirs.add(path)
        if parents:
            parts = path.strip("/").split("/")
            for i in range(1, len(parts)):
                self._dirs.add("/" + "/".join(parts[:i]))
        return HandlerResponse.ok(data=None, backend_name="mock")

    def rmdir(
        self, path: str, recursive: bool = False, context: Any = None
    ) -> HandlerResponse[None]:
        if path not in self._dirs:
            return HandlerResponse.not_found(
                path=path, message="Directory not found", backend_name="mock"
            )
        self._dirs.discard(path)
        return HandlerResponse.ok(data=None, backend_name="mock")

    def is_directory(self, path: str, context: Any = None) -> HandlerResponse[bool]:
        return HandlerResponse.ok(data=path in self._dirs, backend_name="mock")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_backend() -> _MockBackend:
    return _MockBackend()


@pytest.fixture()
def local_backend(tmp_path: Any) -> Backend:
    from nexus.backends.local import LocalBackend

    return LocalBackend(root_path=str(tmp_path / "nexus-data"))


# ---------------------------------------------------------------------------
# Contract test class — parametrized by backend fixture
# ---------------------------------------------------------------------------

class BackendContractTests:
    """Reusable contract tests for any Backend implementation.

    Subclass or use parametrize to run against different backends.
    """

    # === Content Operations (CAS) ===

    @staticmethod
    def test_write_then_read_roundtrip(backend: Backend) -> None:
        content = b"hello world"
        write_resp = backend.write_content(content)
        assert write_resp.success
        content_hash = write_resp.data

        read_resp = backend.read_content(content_hash)
        assert read_resp.success
        assert read_resp.data == content

    @staticmethod
    def test_content_exists_after_write(backend: Backend) -> None:
        content = b"test exists"
        write_resp = backend.write_content(content)
        content_hash = write_resp.data

        exists_resp = backend.content_exists(content_hash)
        assert exists_resp.success
        assert exists_resp.data is True

    @staticmethod
    def test_content_not_exists_for_unknown(backend: Backend) -> None:
        fake_hash = "0" * 64
        exists_resp = backend.content_exists(fake_hash)
        assert exists_resp.success
        assert exists_resp.data is False

    @staticmethod
    def test_delete_removes_content(backend: Backend) -> None:
        content = b"delete me"
        write_resp = backend.write_content(content)
        content_hash = write_resp.data

        del_resp = backend.delete_content(content_hash)
        assert del_resp.success

        exists_resp = backend.content_exists(content_hash)
        assert exists_resp.data is False

    @staticmethod
    def test_write_is_idempotent(backend: Backend) -> None:
        """CAS: writing same content twice returns same hash."""
        content = b"idempotent data"
        hash1 = backend.write_content(content).data
        hash2 = backend.write_content(content).data
        assert hash1 == hash2

    @staticmethod
    def test_get_content_size_correct(backend: Backend) -> None:
        content = b"measure me"
        write_resp = backend.write_content(content)
        content_hash = write_resp.data

        size_resp = backend.get_content_size(content_hash)
        assert size_resp.success
        assert size_resp.data == len(content)

    @staticmethod
    def test_get_ref_count_after_writes(backend: Backend) -> None:
        content = b"ref counting"
        backend.write_content(content)
        write_resp = backend.write_content(content)  # Second write
        content_hash = write_resp.data

        ref_resp = backend.get_ref_count(content_hash)
        assert ref_resp.success
        assert ref_resp.data >= 2

    @staticmethod
    def test_read_nonexistent_content_fails(backend: Backend) -> None:
        fake_hash = "a" * 64
        read_resp = backend.read_content(fake_hash)
        assert not read_resp.success

    @staticmethod
    def test_delete_nonexistent_is_safe(backend: Backend) -> None:
        """Deleting non-existent content should not raise."""
        fake_hash = "b" * 64
        del_resp = backend.delete_content(fake_hash)
        # Should return not_found but not crash
        assert not del_resp.success

    @staticmethod
    def test_batch_read_partial_missing(backend: Backend) -> None:
        content = b"batch test"
        write_resp = backend.write_content(content)
        real_hash = write_resp.data
        fake_hash = "c" * 64

        results = backend.batch_read_content([real_hash, fake_hash])
        assert results[real_hash] == content
        assert results[fake_hash] is None

    # === Directory Operations ===

    @staticmethod
    def test_mkdir_creates_directory(backend: Backend) -> None:
        resp = backend.mkdir("/testdir", exist_ok=True)
        assert resp.success

        is_dir_resp = backend.is_directory("/testdir")
        assert is_dir_resp.success
        assert is_dir_resp.data is True

    @staticmethod
    def test_rmdir_removes_directory(backend: Backend) -> None:
        backend.mkdir("/toremove", exist_ok=True)
        resp = backend.rmdir("/toremove")
        assert resp.success

        is_dir_resp = backend.is_directory("/toremove")
        assert is_dir_resp.data is False

    @staticmethod
    def test_is_directory_false_for_nonexistent(backend: Backend) -> None:
        resp = backend.is_directory("/nonexistent")
        assert resp.success
        assert resp.data is False

    @staticmethod
    def test_mkdir_parents(backend: Backend) -> None:
        resp = backend.mkdir("/a/b/c", parents=True, exist_ok=True)
        assert resp.success

    @staticmethod
    def test_mkdir_exist_ok(backend: Backend) -> None:
        backend.mkdir("/existing", exist_ok=True)
        resp = backend.mkdir("/existing", exist_ok=True)
        assert resp.success

    # === Capability Flags ===

    @staticmethod
    def test_capability_flags_are_booleans(backend: Backend) -> None:
        assert isinstance(backend.user_scoped, bool)
        assert isinstance(backend.is_connected, bool)
        assert isinstance(backend.is_passthrough, bool)
        assert isinstance(backend.has_root_path, bool)
        assert isinstance(backend.has_virtual_filesystem, bool)
        assert isinstance(backend.has_token_manager, bool)

    @staticmethod
    def test_name_returns_string(backend: Backend) -> None:
        assert isinstance(backend.name, str)
        assert len(backend.name) > 0

    # === Connection Lifecycle ===

    @staticmethod
    def test_connect_returns_status(backend: Backend) -> None:
        resp = backend.connect()
        assert resp.success is True

    @staticmethod
    def test_check_connection_returns_status(backend: Backend) -> None:
        resp = backend.check_connection()
        assert isinstance(resp.success, bool)

    @staticmethod
    def test_disconnect_does_not_raise(backend: Backend) -> None:
        backend.disconnect()  # Should not raise

    # === Protocol Conformance ===

    @staticmethod
    def test_backend_satisfies_content_store_protocol(backend: Backend) -> None:
        assert isinstance(backend, ContentStoreProtocol)

    @staticmethod
    def test_backend_satisfies_directory_ops_protocol(backend: Backend) -> None:
        assert isinstance(backend, DirectoryOpsProtocol)

    @staticmethod
    def test_backend_satisfies_connector_protocol(backend: Backend) -> None:
        assert isinstance(backend, ConnectorProtocol)

    # === Context Passthrough ===

    @staticmethod
    def test_write_with_context(backend: Backend) -> None:
        """Passing context=None should not crash."""
        resp = backend.write_content(b"ctx test", context=None)
        assert resp.success

    @staticmethod
    def test_read_with_context(backend: Backend) -> None:
        write_resp = backend.write_content(b"ctx read", context=None)
        read_resp = backend.read_content(write_resp.data, context=None)
        assert read_resp.success


# ---------------------------------------------------------------------------
# Run contract tests against MockBackend
# ---------------------------------------------------------------------------

class TestMockBackendContract:
    """Contract tests against MockBackend (reference implementation)."""

    @pytest.fixture(autouse=True)
    def _setup(self, mock_backend: _MockBackend) -> None:
        self.backend = mock_backend

    def test_write_then_read_roundtrip(self) -> None:
        BackendContractTests.test_write_then_read_roundtrip(self.backend)

    def test_content_exists_after_write(self) -> None:
        BackendContractTests.test_content_exists_after_write(self.backend)

    def test_content_not_exists_for_unknown(self) -> None:
        BackendContractTests.test_content_not_exists_for_unknown(self.backend)

    def test_delete_removes_content(self) -> None:
        BackendContractTests.test_delete_removes_content(self.backend)

    def test_write_is_idempotent(self) -> None:
        BackendContractTests.test_write_is_idempotent(self.backend)

    def test_get_content_size_correct(self) -> None:
        BackendContractTests.test_get_content_size_correct(self.backend)

    def test_get_ref_count_after_writes(self) -> None:
        BackendContractTests.test_get_ref_count_after_writes(self.backend)

    def test_read_nonexistent_content_fails(self) -> None:
        BackendContractTests.test_read_nonexistent_content_fails(self.backend)

    def test_delete_nonexistent_is_safe(self) -> None:
        BackendContractTests.test_delete_nonexistent_is_safe(self.backend)

    def test_batch_read_partial_missing(self) -> None:
        BackendContractTests.test_batch_read_partial_missing(self.backend)

    def test_mkdir_creates_directory(self) -> None:
        BackendContractTests.test_mkdir_creates_directory(self.backend)

    def test_rmdir_removes_directory(self) -> None:
        BackendContractTests.test_rmdir_removes_directory(self.backend)

    def test_is_directory_false_for_nonexistent(self) -> None:
        BackendContractTests.test_is_directory_false_for_nonexistent(self.backend)

    def test_mkdir_parents(self) -> None:
        BackendContractTests.test_mkdir_parents(self.backend)

    def test_mkdir_exist_ok(self) -> None:
        BackendContractTests.test_mkdir_exist_ok(self.backend)

    def test_capability_flags_are_booleans(self) -> None:
        BackendContractTests.test_capability_flags_are_booleans(self.backend)

    def test_name_returns_string(self) -> None:
        BackendContractTests.test_name_returns_string(self.backend)

    def test_connect_returns_status(self) -> None:
        BackendContractTests.test_connect_returns_status(self.backend)

    def test_check_connection_returns_status(self) -> None:
        BackendContractTests.test_check_connection_returns_status(self.backend)

    def test_disconnect_does_not_raise(self) -> None:
        BackendContractTests.test_disconnect_does_not_raise(self.backend)

    def test_backend_satisfies_content_store_protocol(self) -> None:
        BackendContractTests.test_backend_satisfies_content_store_protocol(self.backend)

    def test_backend_satisfies_directory_ops_protocol(self) -> None:
        BackendContractTests.test_backend_satisfies_directory_ops_protocol(self.backend)

    def test_backend_satisfies_connector_protocol(self) -> None:
        BackendContractTests.test_backend_satisfies_connector_protocol(self.backend)

    def test_write_with_context(self) -> None:
        BackendContractTests.test_write_with_context(self.backend)

    def test_read_with_context(self) -> None:
        BackendContractTests.test_read_with_context(self.backend)


# ---------------------------------------------------------------------------
# Run contract tests against LocalBackend (real filesystem)
# ---------------------------------------------------------------------------

class TestLocalBackendContract:
    """Contract tests against LocalBackend (real filesystem)."""

    @pytest.fixture(autouse=True)
    def _setup(self, local_backend: Backend) -> None:
        self.backend = local_backend

    def test_write_then_read_roundtrip(self) -> None:
        BackendContractTests.test_write_then_read_roundtrip(self.backend)

    def test_content_exists_after_write(self) -> None:
        BackendContractTests.test_content_exists_after_write(self.backend)

    def test_content_not_exists_for_unknown(self) -> None:
        BackendContractTests.test_content_not_exists_for_unknown(self.backend)

    def test_delete_removes_content(self) -> None:
        BackendContractTests.test_delete_removes_content(self.backend)

    def test_write_is_idempotent(self) -> None:
        BackendContractTests.test_write_is_idempotent(self.backend)

    def test_get_content_size_correct(self) -> None:
        BackendContractTests.test_get_content_size_correct(self.backend)

    def test_get_ref_count_after_writes(self) -> None:
        BackendContractTests.test_get_ref_count_after_writes(self.backend)

    def test_read_nonexistent_content_fails(self) -> None:
        BackendContractTests.test_read_nonexistent_content_fails(self.backend)

    def test_delete_nonexistent_is_safe(self) -> None:
        BackendContractTests.test_delete_nonexistent_is_safe(self.backend)

    def test_batch_read_partial_missing(self) -> None:
        BackendContractTests.test_batch_read_partial_missing(self.backend)

    def test_mkdir_creates_directory(self) -> None:
        BackendContractTests.test_mkdir_creates_directory(self.backend)

    def test_rmdir_removes_directory(self) -> None:
        BackendContractTests.test_rmdir_removes_directory(self.backend)

    def test_is_directory_false_for_nonexistent(self) -> None:
        BackendContractTests.test_is_directory_false_for_nonexistent(self.backend)

    def test_mkdir_parents(self) -> None:
        BackendContractTests.test_mkdir_parents(self.backend)

    def test_mkdir_exist_ok(self) -> None:
        BackendContractTests.test_mkdir_exist_ok(self.backend)

    def test_capability_flags_are_booleans(self) -> None:
        BackendContractTests.test_capability_flags_are_booleans(self.backend)

    def test_name_returns_string(self) -> None:
        BackendContractTests.test_name_returns_string(self.backend)

    def test_connect_returns_status(self) -> None:
        BackendContractTests.test_connect_returns_status(self.backend)

    def test_check_connection_returns_status(self) -> None:
        BackendContractTests.test_check_connection_returns_status(self.backend)

    def test_disconnect_does_not_raise(self) -> None:
        BackendContractTests.test_disconnect_does_not_raise(self.backend)

    def test_backend_satisfies_content_store_protocol(self) -> None:
        BackendContractTests.test_backend_satisfies_content_store_protocol(self.backend)

    def test_backend_satisfies_directory_ops_protocol(self) -> None:
        BackendContractTests.test_backend_satisfies_directory_ops_protocol(self.backend)

    def test_backend_satisfies_connector_protocol(self) -> None:
        BackendContractTests.test_backend_satisfies_connector_protocol(self.backend)

    def test_write_with_context(self) -> None:
        BackendContractTests.test_write_with_context(self.backend)

    def test_read_with_context(self) -> None:
        BackendContractTests.test_read_with_context(self.backend)

    def test_local_has_root_path(self) -> None:
        """LocalBackend-specific: has_root_path should be True."""
        assert self.backend.has_root_path is True
