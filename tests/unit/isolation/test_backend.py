"""Unit tests for IsolatedBackend — conformance and isolation behaviour."""

from __future__ import annotations

import hashlib

import pytest

from nexus.backends.backend import Backend
from nexus.isolation import IsolatedBackend, IsolationConfig, create_isolated_backend

# conftest.MockBackend path
_MOD = "tests.unit.isolation.conftest"
_CLS = "MockBackend"


def _cfg(**overrides: object) -> IsolationConfig:
    defaults: dict = {
        "backend_module": _MOD,
        "backend_class": _CLS,
        "pool_size": 1,
        "call_timeout": 30.0,
        "startup_timeout": 30.0,
        "force_process": True,
    }
    defaults.update(overrides)
    return IsolationConfig(**defaults)


@pytest.fixture()
def isolated() -> IsolatedBackend:
    backend = IsolatedBackend(_cfg())
    yield backend  # type: ignore[misc]
    backend.disconnect()


# ═══════════════════════════════════════════════════════════════════════
# Conformance tests — IsolatedBackend behaves like any Backend
# ═══════════════════════════════════════════════════════════════════════


class TestConformance:
    """IsolatedBackend implements the Backend ABC correctly."""

    def test_is_backend_subclass(self) -> None:
        assert issubclass(IsolatedBackend, Backend)

    def test_isinstance(self, isolated: IsolatedBackend) -> None:
        assert isinstance(isolated, Backend)

    def test_write_read_roundtrip(self, isolated: IsolatedBackend) -> None:
        data = b"hello isolation"
        wr = isolated.write_content(data)
        assert wr.success is True
        expected_hash = hashlib.sha256(data).hexdigest()
        assert wr.data == expected_hash

        rd = isolated.read_content(wr.data)
        assert rd.success is True
        assert rd.data == data

    def test_delete_and_exists(self, isolated: IsolatedBackend) -> None:
        wr = isolated.write_content(b"to-delete")
        assert isolated.content_exists(wr.data).data is True

        isolated.delete_content(wr.data)
        assert isolated.content_exists(wr.data).data is False

    def test_get_content_size(self, isolated: IsolatedBackend) -> None:
        content = b"12345"
        wr = isolated.write_content(content)
        size_resp = isolated.get_content_size(wr.data)
        assert size_resp.success is True
        assert size_resp.data == 5

    def test_get_ref_count(self, isolated: IsolatedBackend) -> None:
        wr = isolated.write_content(b"ref-test")
        rc = isolated.get_ref_count(wr.data)
        assert rc.success is True
        assert rc.data >= 1

    def test_mkdir_rmdir_is_directory(self, isolated: IsolatedBackend) -> None:
        mk = isolated.mkdir("/testdir", exist_ok=True)
        assert mk.success is True

        is_dir = isolated.is_directory("/testdir")
        assert is_dir.data is True

        rm = isolated.rmdir("/testdir")
        assert rm.success is True

        is_dir2 = isolated.is_directory("/testdir")
        assert is_dir2.data is False


class TestStreamingBuffered:
    def test_stream_content(self, isolated: IsolatedBackend) -> None:
        data = b"A" * 1000
        wr = isolated.write_content(data)
        chunks = list(isolated.stream_content(wr.data, chunk_size=300))
        assert b"".join(chunks) == data
        assert len(chunks) == 4  # 300+300+300+100

    def test_write_stream(self, isolated: IsolatedBackend) -> None:
        parts = [b"chunk1", b"chunk2", b"chunk3"]
        wr = isolated.write_stream(iter(parts))
        assert wr.success is True

        full = b"chunk1chunk2chunk3"
        expected_hash = hashlib.sha256(full).hexdigest()
        assert wr.data == expected_hash

        rd = isolated.read_content(wr.data)
        assert rd.data == full


class TestProperties:
    def test_name_wraps(self, isolated: IsolatedBackend) -> None:
        assert isolated.name == "isolated(mock)"

    def test_user_scoped(self, isolated: IsolatedBackend) -> None:
        assert isolated.user_scoped is False

    def test_thread_safe(self, isolated: IsolatedBackend) -> None:
        assert isolated.thread_safe is True

    def test_supports_rename(self, isolated: IsolatedBackend) -> None:
        assert isolated.supports_rename is False

    def test_has_virtual_filesystem(self, isolated: IsolatedBackend) -> None:
        assert isolated.has_virtual_filesystem is False

    def test_is_connected(self, isolated: IsolatedBackend) -> None:
        assert isolated.is_connected is True

    def test_property_caching(self, isolated: IsolatedBackend) -> None:
        _ = isolated.name
        _ = isolated.name  # should hit cache
        # Verify cache was populated
        assert "name" in isolated._prop_cache


# ═══════════════════════════════════════════════════════════════════════
# Isolation behaviour tests
# ═══════════════════════════════════════════════════════════════════════


class TestErrorHandling:
    def test_read_nonexistent_returns_error(self, isolated: IsolatedBackend) -> None:
        resp = isolated.read_content("nonexistent_hash")
        assert resp.success is False

    def test_list_dir_not_found_raises(self, isolated: IsolatedBackend) -> None:
        with pytest.raises(FileNotFoundError):
            isolated.list_dir("/no-such-dir")


class TestConnectionLifecycle:
    def test_connect(self, isolated: IsolatedBackend) -> None:
        status = isolated.connect()
        assert status.success is True

    def test_disconnect(self, isolated: IsolatedBackend) -> None:
        isolated.disconnect()
        assert isolated.is_connected is False

    def test_connect_bad_module(self) -> None:
        bad = IsolatedBackend(_cfg(backend_module="no.such.module"))
        status = bad.connect()
        assert status.success is False
        bad.disconnect()


class TestFactoryFunction:
    def test_create_isolated_backend(self) -> None:
        backend = create_isolated_backend(
            _MOD,
            _CLS,
            pool_size=1,
            call_timeout=30.0,
            force_process=True,
        )
        try:
            assert isinstance(backend, IsolatedBackend)
            assert isinstance(backend, Backend)
            wr = backend.write_content(b"factory-test")
            assert wr.success is True
        finally:
            backend.disconnect()
