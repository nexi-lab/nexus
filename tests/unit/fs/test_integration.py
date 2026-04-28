"""Integration test: boot-to-read/write lifecycle with real storage.

Uses real SQLite metadata store + real CASLocalBackend in a temp directory.
No mocks — this verifies the full slim package actually works end-to-end.

Test plan:
1. Boot slim NexusFS with SQLite + CASLocalBackend
2. Write a file, read it back
3. Stat the file
4. List directory
5. Rename the file
6. Delete the file
7. Verify deleted
8. Multi-backend: mount two local backends, write to each
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.metadata import DT_MOUNT  # noqa: E402
from nexus.contracts.types import OperationContext
from nexus.core.config import PermissionConfig
from nexus.core.nexus_fs import NexusFS
from nexus.fs import _make_mount_entry
from nexus.fs._helpers import LOCAL_CONTEXT, list_mounts
from nexus.fs._sqlite_meta import SQLiteMetastore


@pytest.fixture
def slim_fs(tmp_path: Path):
    """Boot a full slim NexusFS with SQLite + CASLocalBackend."""
    # SQLite metastore
    db_path = str(tmp_path / "metadata.db")
    metastore = SQLiteMetastore(db_path)

    # CASLocalBackend
    from nexus.backends.storage.cas_local import CASLocalBackend

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    backend = CASLocalBackend(root_path=data_dir)

    # Kernel (constructs its own DriverLifecycleCoordinator + VFS routing)
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

    # Mount via coordinator (registers in backend pool + routing table + hooks)
    kernel.sys_setattr("/local", entry_type=DT_MOUNT, backend=backend)

    # Create DT_MOUNT entry so stat("/local") works
    metastore.put(_make_mount_entry("/local", backend.name))

    return kernel


@pytest.fixture
def dual_fs(tmp_path: Path):
    """Boot slim NexusFS with two local backends."""
    from nexus.backends.storage.cas_local import CASLocalBackend

    db_path = str(tmp_path / "metadata.db")
    metastore = SQLiteMetastore(db_path)

    data_a = tmp_path / "data_a"
    data_a.mkdir()
    data_b = tmp_path / "data_b"
    data_b.mkdir()

    backend_a = CASLocalBackend(root_path=data_a)
    backend_b = CASLocalBackend(root_path=data_b)

    # Give each backend a unique name to avoid pool key collision.
    # CASLocalBackend.name hardcodes "local"; when two instances share
    # the same pool key, resolve_backend() returns whichever was last
    # registered. Creating thin subclasses gives distinct pool keys.
    class _BackendA(CASLocalBackend):
        @property
        def name(self) -> str:
            return "local_a"

    class _BackendB(CASLocalBackend):
        @property
        def name(self) -> str:
            return "local_b"

    backend_a.__class__ = _BackendA
    backend_b.__class__ = _BackendB

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

    # Mount via coordinator (registers in backend pool + routing table + hooks)
    kernel.sys_setattr("/a", entry_type=DT_MOUNT, backend=backend_a)
    kernel.sys_setattr("/b", entry_type=DT_MOUNT, backend=backend_b)

    # Create DT_MOUNT entries
    for mp, be in [("/a", backend_a), ("/b", backend_b)]:
        metastore.put(_make_mount_entry(mp, be.name))

    return kernel


# ---------------------------------------------------------------------------
# Single-backend lifecycle
# ---------------------------------------------------------------------------


class TestSingleBackendLifecycle:
    def test_write_and_read(self, slim_fs: NexusFS):
        """Write content, read it back, verify match."""
        content = b"Hello, nexus-fs!"
        slim_fs.write("/local/test.txt", content, context=LOCAL_CONTEXT)
        result = slim_fs.sys_read("/local/test.txt", context=LOCAL_CONTEXT)
        assert result == content

    def test_stat(self, slim_fs: NexusFS):
        """Write a file, stat it, verify metadata."""
        slim_fs.write("/local/meta.txt", b"metadata test", context=LOCAL_CONTEXT)
        stat = slim_fs.sys_stat("/local/meta.txt", context=LOCAL_CONTEXT)
        assert stat is not None
        assert stat["path"] == "/local/meta.txt"
        assert stat["size"] == 13
        assert stat["is_directory"] is False

    def test_ls(self, slim_fs: NexusFS):
        """Write files, list directory, verify they appear."""
        slim_fs.write("/local/a.txt", b"aaa", context=LOCAL_CONTEXT)
        slim_fs.write("/local/b.txt", b"bbb", context=LOCAL_CONTEXT)
        entries = list(
            slim_fs.sys_readdir("/local/", recursive=True, details=False, context=LOCAL_CONTEXT)
        )
        paths = [e for e in entries if e.endswith(".txt")]
        assert "/local/a.txt" in paths
        assert "/local/b.txt" in paths

    def test_exists(self, slim_fs: NexusFS):
        """Check exists before and after write."""
        assert not slim_fs.access("/local/nofile.txt", context=LOCAL_CONTEXT)
        slim_fs.write("/local/nofile.txt", b"now I exist", context=LOCAL_CONTEXT)
        assert slim_fs.access("/local/nofile.txt", context=LOCAL_CONTEXT)

    def test_rename(self, slim_fs: NexusFS):
        """Write, rename, verify old path gone and new path exists."""
        slim_fs.write("/local/old.txt", b"rename me", context=LOCAL_CONTEXT)
        slim_fs.sys_rename("/local/old.txt", "/local/new.txt", context=LOCAL_CONTEXT)
        result = slim_fs.sys_read("/local/new.txt", context=LOCAL_CONTEXT)
        assert result == b"rename me"

    def test_delete(self, slim_fs: NexusFS):
        """Write, delete, verify gone."""
        slim_fs.write("/local/delete-me.txt", b"bye", context=LOCAL_CONTEXT)
        slim_fs.sys_unlink("/local/delete-me.txt", context=LOCAL_CONTEXT)
        stat = slim_fs.sys_stat("/local/delete-me.txt", context=LOCAL_CONTEXT)
        assert stat is None

    def test_copy(self, slim_fs: NexusFS):
        """Write, copy, verify both exist with same content."""
        slim_fs.write("/local/src.txt", b"copy me", context=LOCAL_CONTEXT)
        slim_fs.sys_copy("/local/src.txt", "/local/dst.txt", context=LOCAL_CONTEXT)
        src = slim_fs.sys_read("/local/src.txt", context=LOCAL_CONTEXT)
        dst = slim_fs.sys_read("/local/dst.txt", context=LOCAL_CONTEXT)
        assert src == dst == b"copy me"

    def test_mkdir(self, slim_fs: NexusFS):
        """Create directory, verify it's a directory."""
        slim_fs.mkdir("/local/subdir", parents=True, exist_ok=True, context=LOCAL_CONTEXT)
        stat = slim_fs.sys_stat("/local/subdir", context=LOCAL_CONTEXT)
        assert stat is not None
        assert stat["is_directory"] is True

    def test_stat_directory(self, slim_fs: NexusFS):
        """Stat on the mount root should return directory."""
        stat = slim_fs.sys_stat("/local", context=LOCAL_CONTEXT)
        assert stat is not None
        assert stat["is_directory"] is True

    def test_overwrite(self, slim_fs: NexusFS):
        """Writing to the same path should overwrite."""
        slim_fs.write("/local/ow.txt", b"version 1", context=LOCAL_CONTEXT)
        slim_fs.write("/local/ow.txt", b"version 2", context=LOCAL_CONTEXT)
        result = slim_fs.sys_read("/local/ow.txt", context=LOCAL_CONTEXT)
        assert result == b"version 2"

    def test_binary_content(self, slim_fs: NexusFS):
        """Write and read binary content."""
        content = bytes(range(256))
        slim_fs.write("/local/binary.bin", content, context=LOCAL_CONTEXT)
        result = slim_fs.sys_read("/local/binary.bin", context=LOCAL_CONTEXT)
        assert result == content

    def test_empty_file(self, slim_fs: NexusFS):
        """Write and read empty file."""
        slim_fs.write("/local/empty.txt", b"", context=LOCAL_CONTEXT)
        result = slim_fs.sys_read("/local/empty.txt", context=LOCAL_CONTEXT)
        assert result == b""

    def test_list_mounts(self, slim_fs: NexusFS):
        """Verify mount points are listed."""
        mounts = list_mounts(slim_fs)
        assert "/local" in mounts


# ---------------------------------------------------------------------------
# Edit operations
# ---------------------------------------------------------------------------


class TestEditOperations:
    """Test the edit() method on the kernel."""

    def test_edit_simple_replacement(self, slim_fs: NexusFS):
        """Simple search/replace edit."""
        slim_fs.write("/local/code.py", b"def foo():\n    return 1\n", context=LOCAL_CONTEXT)

        result = slim_fs.edit(
            "/local/code.py", [("def foo():", "def bar():")], context=LOCAL_CONTEXT
        )

        assert result["success"] is True
        assert result["applied_count"] == 1
        content = slim_fs.sys_read("/local/code.py", context=LOCAL_CONTEXT)
        assert b"def bar():" in content
        assert b"def foo():" not in content

    def test_edit_multiple_replacements(self, slim_fs: NexusFS):
        """Multiple edits applied in sequence."""
        slim_fs.write("/local/multi.py", b"x = 1\ny = 2\nz = 3\n", context=LOCAL_CONTEXT)

        result = slim_fs.edit(
            "/local/multi.py",
            [("x = 1", "x = 10"), ("y = 2", "y = 20")],
            context=LOCAL_CONTEXT,
        )

        assert result["success"] is True
        assert result["applied_count"] == 2
        content = slim_fs.sys_read("/local/multi.py", context=LOCAL_CONTEXT)
        assert content == b"x = 10\ny = 20\nz = 3\n"

    def test_edit_returns_diff(self, slim_fs: NexusFS):
        """Edit result includes a unified diff."""
        slim_fs.write("/local/diff.txt", b"hello world\n", context=LOCAL_CONTEXT)

        result = slim_fs.edit("/local/diff.txt", [("hello", "goodbye")], context=LOCAL_CONTEXT)

        assert result["success"] is True
        assert "-hello world" in result["diff"]
        assert "+goodbye world" in result["diff"]

    def test_edit_preview_does_not_modify(self, slim_fs: NexusFS):
        """Preview mode returns diff but doesn't write."""
        original = b"keep me unchanged\n"
        slim_fs.write("/local/preview.txt", original, context=LOCAL_CONTEXT)

        result = slim_fs.edit(
            "/local/preview.txt",
            [("keep me unchanged", "I was changed")],
            context=LOCAL_CONTEXT,
            preview=True,
        )

        assert result["success"] is True
        assert "+I was changed" in result["diff"]
        # File should NOT have changed
        content = slim_fs.sys_read("/local/preview.txt", context=LOCAL_CONTEXT)
        assert content == original

    def test_edit_no_match_fails(self, slim_fs: NexusFS):
        """Edit fails when search string not found."""
        slim_fs.write("/local/nomatch.txt", b"actual content\n", context=LOCAL_CONTEXT)

        result = slim_fs.edit(
            "/local/nomatch.txt",
            [("nonexistent text", "replacement")],
            context=LOCAL_CONTEXT,
            fuzzy_threshold=1.0,
        )

        assert result["success"] is False
        assert len(result["errors"]) > 0

    def test_edit_with_dict_format(self, slim_fs: NexusFS):
        """Edit accepts dict format with old_str/new_str keys."""
        slim_fs.write("/local/dict.txt", b"old value\n", context=LOCAL_CONTEXT)

        result = slim_fs.edit(
            "/local/dict.txt",
            [{"old_str": "old value", "new_str": "new value"}],
            context=LOCAL_CONTEXT,
        )

        assert result["success"] is True
        content = slim_fs.sys_read("/local/dict.txt", context=LOCAL_CONTEXT)
        assert content == b"new value\n"

    def test_edit_fuzzy_match(self, slim_fs: NexusFS):
        """Fuzzy matching handles minor differences."""
        slim_fs.write(
            "/local/fuzzy.py",
            b"def calculate_total(items):\n    return sum(items)\n",
            context=LOCAL_CONTEXT,
        )

        result = slim_fs.edit(
            "/local/fuzzy.py",
            [("def calcuate_total(items):", "def compute_sum(items):")],
            context=LOCAL_CONTEXT,
            fuzzy_threshold=0.8,
        )

        assert result["success"] is True
        assert result["matches"][0]["match_type"] == "fuzzy"
        content = slim_fs.sys_read("/local/fuzzy.py", context=LOCAL_CONTEXT)
        assert b"def compute_sum(items):" in content

    def test_edit_content_id_concurrency(self, slim_fs: NexusFS):
        """Optimistic concurrency: edit with correct content_id succeeds."""
        write_result = slim_fs.write("/local/etag.txt", b"version 1\n", context=LOCAL_CONTEXT)
        content_id = write_result["content_id"]

        result = slim_fs.edit(
            "/local/etag.txt",
            [("version 1", "version 2")],
            context=LOCAL_CONTEXT,
            if_match=content_id,
        )

        assert result["success"] is True
        content = slim_fs.sys_read("/local/etag.txt", context=LOCAL_CONTEXT)
        assert content == b"version 2\n"

    def test_edit_stale_content_id_fails(self, slim_fs: NexusFS):
        """Optimistic concurrency: stale content_id is rejected."""
        write_result = slim_fs.write("/local/stale.txt", b"version 1\n", context=LOCAL_CONTEXT)
        old_content_id = write_result["content_id"]

        # Overwrite to change the content_id
        slim_fs.write("/local/stale.txt", b"version 2\n", context=LOCAL_CONTEXT)

        from nexus.contracts.exceptions import ConflictError

        with pytest.raises(ConflictError):
            slim_fs.edit(
                "/local/stale.txt",
                [("version 2", "version 3")],
                context=LOCAL_CONTEXT,
                if_match=old_content_id,
            )

    def test_edit_delete_text(self, slim_fs: NexusFS):
        """Replace with empty string to delete text."""
        slim_fs.write("/local/del.txt", b"keep\nremove me\nkeep too\n", context=LOCAL_CONTEXT)

        result = slim_fs.edit("/local/del.txt", [("remove me\n", "")], context=LOCAL_CONTEXT)

        assert result["success"] is True
        content = slim_fs.sys_read("/local/del.txt", context=LOCAL_CONTEXT)
        assert content == b"keep\nkeep too\n"

    def test_edit_multiline_block(self, slim_fs: NexusFS):
        """Edit a multiline block."""
        slim_fs.write(
            "/local/block.py",
            b"def old():\n    pass\n\ndef other():\n    pass\n",
            context=LOCAL_CONTEXT,
        )

        result = slim_fs.edit(
            "/local/block.py",
            [("def old():\n    pass", "def new():\n    return 42")],
            context=LOCAL_CONTEXT,
        )

        assert result["success"] is True
        content = slim_fs.sys_read("/local/block.py", context=LOCAL_CONTEXT)
        assert b"def new():\n    return 42" in content
        assert b"def other():\n    pass" in content


# ---------------------------------------------------------------------------
# Multi-backend
# ---------------------------------------------------------------------------


class TestMultiBackend:
    def test_write_to_separate_backends(self, dual_fs: NexusFS):
        """Write to two different backends, verify isolation."""
        dual_fs.write("/a/file.txt", b"backend A", context=LOCAL_CONTEXT)
        dual_fs.write("/b/file.txt", b"backend B", context=LOCAL_CONTEXT)

        assert dual_fs.sys_read("/a/file.txt", context=LOCAL_CONTEXT) == b"backend A"
        assert dual_fs.sys_read("/b/file.txt", context=LOCAL_CONTEXT) == b"backend B"

    def test_cross_backend_copy(self, dual_fs: NexusFS):
        """Copy from one backend to another."""
        dual_fs.write("/a/src.txt", b"cross-copy", context=LOCAL_CONTEXT)
        dual_fs.sys_copy("/a/src.txt", "/b/dst.txt", context=LOCAL_CONTEXT)

        assert dual_fs.sys_read("/b/dst.txt", context=LOCAL_CONTEXT) == b"cross-copy"

    def test_list_multiple_mounts(self, dual_fs: NexusFS):
        """Both mounts should be visible."""
        mounts = list_mounts(dual_fs)
        assert "/a" in mounts
        assert "/b" in mounts


# ---------------------------------------------------------------------------
# SQLite metastore
# ---------------------------------------------------------------------------


class TestSQLiteMetastore:
    # F3 C4: the stdlib-SQLite backend was replaced with a kernel-backed
    # RustMetastoreProxy factory under the same import name. The previous
    # ``test_wal_mode_enabled`` check (``PRAGMA journal_mode == 'wal'``)
    # was specific to the sqlite3 implementation and is removed with the
    # backing store.

    def test_put_and_get(self, tmp_path: Path):
        """Basic put/get on the SQLite metastore."""
        from datetime import UTC, datetime

        from nexus.contracts.metadata import FileMetadata

        db_path = str(tmp_path / "test.db")
        meta = SQLiteMetastore(db_path)

        fm = FileMetadata(
            path="/test/file.txt",
            size=42,
            content_id="abc123",
            mime_type="text/plain",
            created_at=datetime.now(UTC),
            modified_at=datetime.now(UTC),
            version=1,
            zone_id=ROOT_ZONE_ID,
        )
        meta.put(fm)
        result = meta.get("/test/file.txt")
        assert result is not None
        assert result.path == "/test/file.txt"
        assert result.size == 42
