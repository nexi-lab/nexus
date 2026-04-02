"""Tests for SyscallEngine Python backend callback (Phase F — #1868).

Verifies that when CAS is unavailable, Rust calls Python backend.read_content()
and backend.write_content() via PyO3 callback, avoiding the full Python
validate+route+dcache overhead.

GIL safety: Rust clones Py<PyAny> under RwLock, releases lock, then calls Python.
Same pattern as dispatch.rs HookEntry.
"""

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest
from nexus_fast import (
    PathTrie,
    RustDCache,
    RustPathRouter,
    SyscallEngine,
)

# Entry type constants
DT_REG = 0


@dataclass(frozen=True, slots=True)
class WriteResult:
    """Minimal WriteResult for testing."""

    content_id: str
    version: str = ""
    size: int = 0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_backend():
    """Mock ObjectStoreABC-like backend with read_content/write_content."""
    backend = MagicMock()
    backend.name = "mock-s3"
    backend.has_root_path = False
    backend.read_content.return_value = b"data from python backend"
    backend.write_content.return_value = WriteResult(content_id="abc123hash", version="1", size=24)
    return backend


@pytest.fixture
def engine_with_backend(mock_backend):
    """SyscallEngine with a Python backend on the root mount (no CAS)."""
    dcache = RustDCache()
    router = RustPathRouter()
    trie = PathTrie()
    # Mount without local_root (no CAS) but WITH Python backend
    router.add_mount("/", "root", False, False, "balanced", "mock-s3", None, False, mock_backend)
    engine = SyscallEngine(dcache, router, trie)
    return engine, dcache, mock_backend


# ---------------------------------------------------------------------------
# execute_read backend callback
# ---------------------------------------------------------------------------


class TestExecuteReadBackendCallback:
    def test_backend_read_called(self, engine_with_backend):
        """CAS miss + backend present → backend.read_content() called."""
        engine, dcache, backend = engine_with_backend
        dcache.put("/workspace/test.txt", "mock-s3", "test.txt", 100, DT_REG, etag="hash123")

        result = engine.sys_read("/workspace/test.txt", "root", False)
        assert result is not None
        assert result == b"data from python backend"
        backend.read_content.assert_called_once_with("hash123")

    def test_backend_read_returns_bytes(self, engine_with_backend):
        """Backend returns bytes, Rust wraps as PyBytes."""
        engine, dcache, backend = engine_with_backend
        backend.read_content.return_value = b"\x00\x01\x02binary"
        dcache.put("/workspace/bin.dat", "mock-s3", "", 7, DT_REG, etag="binhash")

        result = engine.sys_read("/workspace/bin.dat", "root", False)
        assert result == b"\x00\x01\x02binary"

    def test_dcache_miss_returns_none(self, engine_with_backend):
        """DCache miss → None (backend not called)."""
        engine, _, backend = engine_with_backend
        result = engine.sys_read("/workspace/missing.txt", "root", False)
        assert result is None
        backend.read_content.assert_not_called()

    def test_no_etag_returns_none(self, engine_with_backend):
        """Entry without etag → None (backend not called)."""
        engine, dcache, backend = engine_with_backend
        dcache.put("/workspace/new.txt", "mock-s3", "", 0, DT_REG)
        result = engine.sys_read("/workspace/new.txt", "root", False)
        assert result is None
        backend.read_content.assert_not_called()


# ---------------------------------------------------------------------------
# execute_write backend callback
# ---------------------------------------------------------------------------


class TestExecuteWriteBackendCallback:
    def test_backend_write_called(self, engine_with_backend):
        """CAS miss + backend present → backend.write_content() called."""
        engine, dcache, backend = engine_with_backend
        dcache.put("/workspace/test.txt", "mock-s3", "test.txt", 100, DT_REG, etag="oldhash")

        result = engine.sys_write("/workspace/test.txt", "root", b"new content", False)
        assert result is not None
        assert result == "abc123hash"
        backend.write_content.assert_called_once_with(b"new content")

    def test_backend_write_returns_content_id(self, engine_with_backend):
        """Backend WriteResult.content_id is extracted correctly."""
        engine, dcache, backend = engine_with_backend
        backend.write_content.return_value = WriteResult(content_id="sha256hexhash")
        dcache.put("/workspace/f.txt", "mock-s3", "", 0, DT_REG, etag="old")

        result = engine.sys_write("/workspace/f.txt", "root", b"data", False)
        assert result == "sha256hexhash"

    def test_dcache_miss_returns_none(self, engine_with_backend):
        """DCache miss → None (backend not called)."""
        engine, _, backend = engine_with_backend
        result = engine.sys_write("/workspace/missing.txt", "root", b"data", False)
        assert result is None
        backend.write_content.assert_not_called()


# ---------------------------------------------------------------------------
# CAS preferred over backend
# ---------------------------------------------------------------------------


class TestCASPreferredOverBackend:
    def test_cas_preferred_for_read(self, mock_backend):
        """When CAS has the content, Python backend is NOT called."""
        import tempfile
        from pathlib import Path

        from nexus_fast import hash_bytes

        with tempfile.TemporaryDirectory() as cas_dir:
            dcache = RustDCache()
            router = RustPathRouter()
            trie = PathTrie()
            # Mount WITH CAS (local_root) AND Python backend
            router.add_mount(
                "/", "root", False, False, "balanced", "local", cas_dir, False, mock_backend
            )
            engine = SyscallEngine(dcache, router, trie)

            content = b"cas content"
            content_hash = hash_bytes(content)

            # Write CAS blob manually
            cas_path = Path(cas_dir) / "cas" / content_hash[:2] / content_hash[2:4] / content_hash
            cas_path.parent.mkdir(parents=True, exist_ok=True)
            cas_path.write_bytes(content)

            dcache.put(
                "/workspace/f.txt", "local", content_hash, len(content), DT_REG, etag=content_hash
            )

            result = engine.sys_read("/workspace/f.txt", "root", False)
            assert result == content
            # Backend should NOT be called — CAS served it
            mock_backend.read_content.assert_not_called()

    def test_cas_preferred_for_write(self, mock_backend):
        """When CAS is available, Python backend write is NOT called."""
        import tempfile

        with tempfile.TemporaryDirectory() as cas_dir:
            dcache = RustDCache()
            router = RustPathRouter()
            trie = PathTrie()
            router.add_mount(
                "/", "root", False, False, "balanced", "local", cas_dir, False, mock_backend
            )
            engine = SyscallEngine(dcache, router, trie)

            dcache.put("/workspace/f.txt", "local", "", 0, DT_REG, etag="old")
            result = engine.sys_write("/workspace/f.txt", "root", b"new data", False)
            assert result is not None
            assert len(result) == 64  # BLAKE3 hex
            mock_backend.write_content.assert_not_called()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestBackendExceptionReturnsNone:
    def test_read_exception_returns_none(self, engine_with_backend):
        """Backend raises → Rust gets Err → returns None → Python full path fallback."""
        engine, dcache, backend = engine_with_backend
        backend.read_content.side_effect = RuntimeError("S3 timeout")
        dcache.put("/workspace/test.txt", "mock-s3", "test.txt", 100, DT_REG, etag="hash123")

        result = engine.sys_read("/workspace/test.txt", "root", False)
        assert result is None  # Not an exception — just None for fallback

    def test_write_exception_returns_none(self, engine_with_backend):
        """Backend write raises → returns None → Python full path fallback."""
        engine, dcache, backend = engine_with_backend
        backend.write_content.side_effect = OSError("write failed")
        dcache.put("/workspace/test.txt", "mock-s3", "test.txt", 100, DT_REG, etag="hash123")

        result = engine.sys_write("/workspace/test.txt", "root", b"data", False)
        assert result is None


# ---------------------------------------------------------------------------
# No backend registered
# ---------------------------------------------------------------------------


class TestNoBackendFallback:
    def test_no_backend_read_returns_none(self):
        """Mount without backend (backend=None) → CAS miss → None."""
        dcache = RustDCache()
        router = RustPathRouter()
        trie = PathTrie()
        router.add_mount("/", "root", False, False, "balanced")
        engine = SyscallEngine(dcache, router, trie)
        dcache.put("/workspace/test.txt", "remote", "test.txt", 100, DT_REG, etag="hash")

        result = engine.sys_read("/workspace/test.txt", "root", False)
        assert result is None

    def test_no_backend_write_returns_none(self):
        """Mount without backend → CAS miss → None."""
        dcache = RustDCache()
        router = RustPathRouter()
        trie = PathTrie()
        router.add_mount("/", "root", False, False, "balanced")
        engine = SyscallEngine(dcache, router, trie)
        dcache.put("/workspace/test.txt", "remote", "test.txt", 100, DT_REG, etag="hash")

        result = engine.sys_write("/workspace/test.txt", "root", b"data", False)
        assert result is None
