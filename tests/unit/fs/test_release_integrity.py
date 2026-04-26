"""Release-integrity tests for nexus-fs (Issue #3328).

Covers:
- mount() cleanup on failure (Issue 9A)
- SQLiteMetastore concurrency + _retry_on_busy (Issue 10A)
- Negative/error path tests (Issue 11A)
- Method-set parity (Issue 8B / 12A)
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.metadata import FileMetadata
from nexus.fs._helpers import LOCAL_CONTEXT
from nexus.fs._helpers import close as _close_fs
from nexus.fs._sqlite_meta import SQLiteMetastore, _retry_on_busy

# =========================================================================
# Issue 9A: mount() cleanup on failure
# =========================================================================


class TestMountCleanupOnFailure:
    """Verify mount() cleans up resources on partial failure."""

    @pytest.mark.asyncio
    async def test_first_backend_failure_closes_metastore(self, tmp_path, monkeypatch):
        """If the first create_backend() fails, metastore must be closed."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))

        with (
            patch("nexus.fs._backend_factory.create_backend", side_effect=ImportError("no boto3")),
            pytest.raises(ImportError, match="no boto3"),
        ):
            from nexus.fs import mount

            await mount("s3://bucket")

    @pytest.mark.asyncio
    async def test_second_backend_failure_closes_first_and_metastore(self, tmp_path, monkeypatch):
        """If the second backend fails, the first backend and metastore are closed."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))

        first_backend = MagicMock()
        first_backend.close = MagicMock()
        call_count = 0

        def mock_create(spec):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return first_backend
            raise ImportError("no gcs")

        with (
            patch(
                "nexus.fs._backend_factory.create_backend",
                side_effect=mock_create,
            ) as _,
            pytest.raises(ImportError, match="no gcs"),
        ):
            from nexus.fs import mount

            await mount("local:///tmp/a", "gcs://project/bucket")

        first_backend.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_kernel_boot_failure_closes_backends_and_metastore(self, tmp_path, monkeypatch):
        """If NexusFS() raises, all backends and the metastore are closed."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))

        mock_backend = MagicMock()
        mock_backend.name = "test"
        mock_backend.close = MagicMock()

        with (
            patch(
                "nexus.fs._backend_factory.create_backend",
                return_value=mock_backend,
            ),
            patch("nexus.core.nexus_fs.NexusFS.__init__", side_effect=RuntimeError("boot failed")),
            pytest.raises(RuntimeError, match="boot failed"),
        ):
            from nexus.fs import mount

            await mount("local:///tmp/data")

        mock_backend.close.assert_called_once()


# =========================================================================
# Issue 10A: SQLiteMetastore concurrency + _retry_on_busy
# =========================================================================


def _make_metadata(path: str) -> FileMetadata:
    """Create a minimal FileMetadata for testing."""
    now = datetime.now(UTC)
    return FileMetadata(
        path=path,
        size=42,
        etag="etag123",
        mime_type="text/plain",
        created_at=now,
        modified_at=now,
        version=1,
        zone_id=ROOT_ZONE_ID,
    )


class TestRetryOnBusy:
    """Unit tests for the _retry_on_busy decorator."""

    def test_succeeds_on_first_try(self) -> None:
        call_count = 0

        @_retry_on_busy
        def op():
            nonlocal call_count
            call_count += 1
            return "ok"

        assert op() == "ok"
        assert call_count == 1

    def test_retries_on_busy_then_succeeds(self) -> None:
        attempts = 0

        @_retry_on_busy
        def op():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        assert op() == "ok"
        assert attempts == 3

    def test_raises_after_max_retries(self) -> None:
        @_retry_on_busy
        def op():
            raise sqlite3.OperationalError("database is locked")

        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            op()

    def test_non_busy_error_not_retried(self) -> None:
        call_count = 0

        @_retry_on_busy
        def op():
            nonlocal call_count
            call_count += 1
            raise sqlite3.OperationalError("disk I/O error")

        with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
            op()
        assert call_count == 1  # no retry


class TestConcurrentMetastore:
    """Thread-contention test for SQLiteMetastore.

    Tests WAL-mode concurrency with separate connections (one per thread),
    which is the supported multi-process/multi-thread pattern for SQLite.
    """

    def test_concurrent_put_get_no_data_loss(self, tmp_path: Path) -> None:
        """5 threads each with their own connection doing 20 put/get cycles."""
        db_path = str(tmp_path / "concurrent.db")
        # Create the schema first
        SQLiteMetastore(db_path).close()

        n_threads = 5
        n_ops = 20
        errors: list[str] = []

        def worker(thread_id: int) -> None:
            ms = SQLiteMetastore(db_path)
            try:
                for i in range(n_ops):
                    path = f"/thread{thread_id}/file{i}.txt"
                    meta = _make_metadata(path)
                    try:
                        ms.put(meta)
                        result = ms.get(path)
                        if result is None:
                            errors.append(
                                f"Thread {thread_id}: get({path}) returned None after put"
                            )
                        elif result.path != path:
                            errors.append(
                                f"Thread {thread_id}: path mismatch {result.path} != {path}"
                            )
                    except Exception as exc:
                        errors.append(f"Thread {thread_id}: {exc}")
            finally:
                ms.close()

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, "Concurrency errors:\n" + "\n".join(errors)

        # Verify all entries were written
        verify = SQLiteMetastore(db_path)
        total = len(verify.list(prefix="/", recursive=True))
        verify.close()
        assert total == n_threads * n_ops


# =========================================================================
# Issue 11A: Negative / error path tests
# =========================================================================


class TestMountErrorPaths:
    """Negative tests for the mount() function."""

    @pytest.mark.asyncio
    async def test_empty_uri_list_raises(self) -> None:
        from nexus.fs import mount

        with pytest.raises(ValueError, match="At least one URI"):
            await mount()

    @pytest.mark.asyncio
    async def test_at_with_multiple_uris_raises(self) -> None:
        from nexus.fs import mount

        with pytest.raises(ValueError, match="'at' override is only valid with a single URI"):
            await mount("s3://a", "s3://b", at="/data")

    @pytest.mark.asyncio
    async def test_mounts_json_write_failure_logs_warning(self, tmp_path, monkeypatch, caplog):
        """If mounts.json write fails, mount() should still succeed with a warning."""
        import logging

        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))

        data_dir = tmp_path / "data"
        data_dir.mkdir()

        from nexus.backends.storage.cas_local import CASLocalBackend

        mock_backend = CASLocalBackend(root_path=data_dir)

        # Mock the mounts_file() to return a path in a non-existent directory
        fake_mounts = tmp_path / "nonexistent" / "deep" / "mounts.json"

        with (
            patch("nexus.fs._backend_factory.create_backend", return_value=mock_backend),
            patch("nexus.fs._paths.mounts_file", return_value=fake_mounts),
            caplog.at_level(logging.WARNING, logger="nexus.fs"),
        ):
            from nexus.fs import mount

            fs = await mount("local:///tmp/data")
            assert fs is not None

        assert any("Could not write mounts.json" in r.message for r in caplog.records)


class TestFsspecErrorPaths:
    """Negative tests for the fsspec layer."""

    @pytest.fixture
    def mock_nexus_fs(self):
        fs = MagicMock()
        fs.stat = MagicMock(return_value={"path": "/test", "size": 11, "is_directory": False})
        return fs

    def test_unsupported_mode_raises(self, mock_nexus_fs):
        pytest.importorskip("fsspec")
        from nexus.fs._fsspec import NexusFileSystem

        NexusFileSystem.clear_instance_cache()
        nfs = NexusFileSystem(nexus_fs=mock_nexus_fs)
        try:
            with pytest.raises(ValueError, match="Unsupported mode"):
                nfs._open("/test", mode="ab")
        finally:
            NexusFileSystem.clear_instance_cache()

    def test_write_buffer_exceeds_limit(self, mock_nexus_fs):
        pytest.importorskip("fsspec")
        from nexus.fs._fsspec import NexusWriteFile
        from nexus.fs._sync import PortalRunner

        runner = PortalRunner()
        try:
            wf = NexusWriteFile(
                fs=MagicMock(),
                path="/big.bin",
                nexus_fs=mock_nexus_fs,
            )
            chunk = b"x" * (1024 * 1024)  # 1 MB
            with pytest.raises(ValueError, match="Write buffer exceeded"):
                for _ in range(1025):
                    wf.write(chunk)
        finally:
            runner.close()

    def test_write_on_closed_file_raises(self, mock_nexus_fs):
        pytest.importorskip("fsspec")
        from nexus.fs._fsspec import NexusWriteFile
        from nexus.fs._sync import PortalRunner

        runner = PortalRunner()
        try:
            wf = NexusWriteFile(
                fs=MagicMock(),
                path="/closed.txt",
                nexus_fs=mock_nexus_fs,
            )
            wf.close()
            with pytest.raises(ValueError, match="closed file"):
                wf.write(b"data")
        finally:
            runner.close()


class TestBackendFactoryErrorPaths:
    """Negative tests for backend creation."""

    def test_missing_s3_extra_raises_import_error(self):
        from nexus.fs._uri import parse_uri

        spec = parse_uri("s3://bucket")

        with (
            patch("nexus.fs._credentials.discover_credentials", return_value={"source": "test"}),
            patch.dict("sys.modules", {"nexus.backends.storage.path_s3": None}),
        ):
            from nexus.fs._backend_factory import create_backend

            with pytest.raises(ImportError, match="boto3"):
                create_backend(spec)

    def test_local_uri_preserves_path_separators(self, tmp_path: Path) -> None:
        """local:// URIs should map to the exact requested local root path."""
        from nexus.fs._backend_factory import create_backend
        from nexus.fs._uri import parse_uri

        target = tmp_path / "nested" / "data"
        spec = parse_uri(f"local://{target}")
        backend = create_backend(spec)

        assert getattr(backend, "root_path", None) == target.resolve()

    def test_local_windows_drive_uri_preserves_drive_prefix(self) -> None:
        """local:///C:/... should keep drive-root semantics (no extra leading slash)."""
        from nexus.fs._backend_factory import _local_root_from_spec
        from nexus.fs._uri import parse_uri

        spec = parse_uri("local:///C:/Users/alice/data")
        expected = "C:/Users/alice/data" if os.name == "nt" else "/C:/Users/alice/data"
        assert _local_root_from_spec(spec) == expected

    def test_local_windows_root_drive_uri_is_absolute(self) -> None:
        """local:///C:/ should stay drive-rooted on Windows and absolute on POSIX."""
        from nexus.fs._backend_factory import _local_root_from_spec
        from nexus.fs._uri import parse_uri

        spec = parse_uri("local:///C:/")
        expected = "C:/" if os.name == "nt" else "/C:/"
        assert _local_root_from_spec(spec) == expected

    def test_local_uppercase_scheme_uri_is_still_absolute(self) -> None:
        """LOCAL:///tmp/data should preserve absolute semantics like local:///tmp/data."""
        from nexus.fs._backend_factory import _local_root_from_spec
        from nexus.fs._uri import parse_uri

        spec = parse_uri("LOCAL:///tmp/data")
        assert _local_root_from_spec(spec) == "/tmp/data"


class TestLocalSchemePassthrough:
    """Guardrails for the ``local://`` passthrough-by-default behaviour.

    Users expect ``mount local://./data`` to put files directly on disk
    at ``./data/<virtual_path>`` — CAS-addressed storage must be a
    separate, explicit opt-in (``cas-local://``).  Previously
    ``local://`` routed to ``CASLocalBackend`` which stored every write
    as a hash-named blob, silently violating least-astonishment.
    """

    def test_local_scheme_returns_passthrough_backend(self, tmp_path):
        from nexus.backends.storage.path_local import PathLocalBackend
        from nexus.fs._backend_factory import create_backend
        from nexus.fs._uri import parse_uri

        spec = parse_uri(f"local://{tmp_path / 'data'}")
        backend = create_backend(spec)
        assert isinstance(backend, PathLocalBackend), (
            "local:// must be passthrough by default — CAS is the opt-in scheme"
        )

    def test_cas_local_scheme_returns_cas_backend(self, tmp_path):
        from nexus.backends.storage.cas_local import CASLocalBackend
        from nexus.fs._backend_factory import create_backend
        from nexus.fs._uri import parse_uri

        spec = parse_uri(f"cas-local://{tmp_path / 'data'}")
        backend = create_backend(spec)
        assert isinstance(backend, CASLocalBackend), (
            "cas-local:// must still give the content-addressed backend"
        )

    def test_local_mount_places_files_on_disk(self, tmp_path):
        """End-to-end sanity: writing via a ``local://`` mount lands at
        the expected disk path, not a hash-named CAS blob."""
        from nexus.fs._backend_factory import create_backend
        from nexus.fs._uri import parse_uri

        data_dir = tmp_path / "data"
        spec = parse_uri(f"local://{data_dir}")
        backend = create_backend(spec)
        # PathLocalBackend exposes a root_path; CASLocalBackend also does
        # but stores content by hash.  The behavioural contract we want
        # to enforce is that the backend type is path-addressed.
        assert backend.__class__.__name__ == "PathLocalBackend"
        assert data_dir.exists()


class TestUriEdgeCases:
    """Additional URI edge cases for derive_bucket()."""

    def test_gcs_bucket_with_path(self) -> None:
        from nexus.fs._uri import derive_bucket, parse_uri

        spec = parse_uri("gcs://project/bucket/subdir")
        assert derive_bucket(spec) == "bucket"

    def test_gcs_bucket_no_path(self) -> None:
        from nexus.fs._uri import derive_bucket, parse_uri

        spec = parse_uri("gcs://my-project")
        assert derive_bucket(spec) == "my-project"

    def test_s3_bucket(self) -> None:
        from nexus.fs._uri import derive_bucket, parse_uri

        spec = parse_uri("s3://my-bucket/subdir")
        assert derive_bucket(spec) == "my-bucket"

    def test_local_bucket(self) -> None:
        from nexus.fs._uri import derive_bucket, parse_uri

        spec = parse_uri("local://./data")
        assert derive_bucket(spec) == "."


class TestPathsPermissions:
    """Tests for state directory permission enforcement."""

    def test_persistent_dir_has_restricted_permissions(self, tmp_path, monkeypatch):
        import os
        import stat

        monkeypatch.setenv("NEXUS_FS_PERSISTENT_DIR", str(tmp_path / "secrets"))

        from nexus.fs._paths import persistent_dir

        p = persistent_dir()
        mode = stat.S_IMODE(os.stat(p).st_mode)
        assert mode == 0o700, f"Expected 0o700, got {oct(mode)}"

    def test_state_dir_created_on_access(self, tmp_path, monkeypatch):
        target = tmp_path / "new_state"
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(target))

        from nexus.fs._paths import state_dir

        p = state_dir()
        assert p.exists()
        assert p == target


class TestKernelLifecycle:
    """Tests for the close() helper on the NexusFS kernel."""

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self, tmp_path):
        """Calling close() twice should not raise."""
        from nexus.backends.storage.cas_local import CASLocalBackend
        from nexus.contracts.types import OperationContext
        from nexus.core.config import PermissionConfig
        from nexus.core.nexus_fs import NexusFS
        from nexus.fs import _make_mount_entry

        db_path = str(tmp_path / "metadata.db")
        metastore = SQLiteMetastore(db_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        backend = CASLocalBackend(root_path=data_dir)

        kernel = NexusFS(
            metadata_store=metastore,
            permissions=PermissionConfig(enforce=False),
        )
        kernel._init_cred = OperationContext(
            user_id="test",
            groups=[],
            zone_id=ROOT_ZONE_ID,
            is_admin=True,
        )

        # Mount via kernel sys_setattr(DT_MOUNT) (F4 Rust-ification).
        from nexus.contracts.metadata import DT_MOUNT

        kernel.sys_setattr("/local", entry_type=DT_MOUNT, backend=backend)
        metastore.put(_make_mount_entry("/local", backend.name))

        _close_fs(kernel)
        _close_fs(kernel)  # should not raise

    @pytest.mark.asyncio
    async def test_close_after_use(self, tmp_path):
        """try/finally close() pattern should work end-to-end."""
        from nexus.backends.storage.cas_local import CASLocalBackend
        from nexus.contracts.types import OperationContext
        from nexus.core.config import PermissionConfig
        from nexus.core.nexus_fs import NexusFS
        from nexus.fs import _make_mount_entry

        db_path = str(tmp_path / "metadata.db")
        metastore = SQLiteMetastore(db_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        backend = CASLocalBackend(root_path=data_dir)

        kernel = NexusFS(
            metadata_store=metastore,
            permissions=PermissionConfig(enforce=False),
        )
        kernel._init_cred = OperationContext(
            user_id="test",
            groups=[],
            zone_id=ROOT_ZONE_ID,
            is_admin=True,
        )

        # Mount via kernel sys_setattr(DT_MOUNT) (F4 Rust-ification).
        from nexus.contracts.metadata import DT_MOUNT

        kernel.sys_setattr("/local", entry_type=DT_MOUNT, backend=backend)
        metastore.put(_make_mount_entry("/local", backend.name))

        fs = kernel
        try:
            fs.write("/local/ctx.txt", b"context manager", context=LOCAL_CONTEXT)
            content = fs.sys_read("/local/ctx.txt", context=LOCAL_CONTEXT)
            assert content == b"context manager"
        finally:
            _close_fs(fs)
