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
from nexus.contracts.types import OperationContext
from nexus.core.config import PermissionConfig
from nexus.core.nexus_fs import NexusFS
from nexus.core.router import PathRouter
from nexus.fs import _make_mount_entry
from nexus.fs._facade import SlimNexusFS
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

    # Router (empty — mounts added via coordinator)
    from nexus.core.mount_table import MountTable

    mount_table = MountTable(metastore)
    router = PathRouter(mount_table)

    # Kernel
    kernel = NexusFS(
        metadata_store=metastore,
        permissions=PermissionConfig(enforce=False),
        router=router,
    )
    kernel._init_cred = OperationContext(
        user_id="test",
        groups=[],
        zone_id=ROOT_ZONE_ID,
        is_admin=True,
    )

    # Mount via coordinator (registers in backend pool + routing table + hooks)
    kernel._driver_coordinator.mount("/local", backend)

    # Create DT_MOUNT entry so stat("/local") works
    metastore.put(_make_mount_entry("/local", backend.name))

    return SlimNexusFS(kernel)


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
    # Class names MUST start with "CAS" so _detect_backend_params in
    # mount_table.py recognises them as CAS backends (avoids gRPC bridge).
    class CASLocalA(CASLocalBackend):
        @property
        def name(self) -> str:
            return "local_a"

    class CASLocalB(CASLocalBackend):
        @property
        def name(self) -> str:
            return "local_b"

    backend_a.__class__ = CASLocalA
    backend_b.__class__ = CASLocalB

    from nexus.core.mount_table import MountTable

    mount_table = MountTable(metastore)
    router = PathRouter(mount_table)

    kernel = NexusFS(
        metadata_store=metastore,
        permissions=PermissionConfig(enforce=False),
        router=router,
    )
    kernel._init_cred = OperationContext(
        user_id="test",
        groups=[],
        zone_id=ROOT_ZONE_ID,
        is_admin=True,
    )

    # Mount via coordinator (registers in backend pool + routing table + hooks)
    kernel._driver_coordinator.mount("/a", backend_a)
    kernel._driver_coordinator.mount("/b", backend_b)

    # Create DT_MOUNT entries
    for mp, be in [("/a", backend_a), ("/b", backend_b)]:
        metastore.put(_make_mount_entry(mp, be.name))

    return SlimNexusFS(kernel)


# ---------------------------------------------------------------------------
# Single-backend lifecycle
# ---------------------------------------------------------------------------


class TestSingleBackendLifecycle:
    def test_write_and_read(self, slim_fs: SlimNexusFS):
        """Write content, read it back, verify match."""
        content = b"Hello, nexus-fs!"
        slim_fs.write("/local/test.txt", content)
        result = slim_fs.read("/local/test.txt")
        assert result == content

    def test_stat(self, slim_fs: SlimNexusFS):
        """Write a file, stat it, verify metadata."""
        slim_fs.write("/local/meta.txt", b"metadata test")
        stat = slim_fs.stat("/local/meta.txt")
        assert stat is not None
        assert stat["path"] == "/local/meta.txt"
        assert stat["size"] == 13
        assert stat["is_directory"] is False

    def test_ls(self, slim_fs: SlimNexusFS):
        """Write files, list directory, verify they appear."""
        slim_fs.write("/local/a.txt", b"aaa")
        slim_fs.write("/local/b.txt", b"bbb")
        entries = slim_fs.ls("/local/", detail=False, recursive=True)
        paths = [e for e in entries if e.endswith(".txt")]
        assert "/local/a.txt" in paths
        assert "/local/b.txt" in paths

    def test_exists(self, slim_fs: SlimNexusFS):
        """Check exists before and after write."""
        assert not slim_fs.exists("/local/nofile.txt")
        slim_fs.write("/local/nofile.txt", b"now I exist")
        assert slim_fs.exists("/local/nofile.txt")

    def test_rename(self, slim_fs: SlimNexusFS):
        """Write, rename, verify old path gone and new path exists."""
        slim_fs.write("/local/old.txt", b"rename me")
        slim_fs.rename("/local/old.txt", "/local/new.txt")
        result = slim_fs.read("/local/new.txt")
        assert result == b"rename me"

    def test_delete(self, slim_fs: SlimNexusFS):
        """Write, delete, verify gone."""
        slim_fs.write("/local/delete-me.txt", b"bye")
        slim_fs.delete("/local/delete-me.txt")
        stat = slim_fs.stat("/local/delete-me.txt")
        assert stat is None

    def test_copy(self, slim_fs: SlimNexusFS):
        """Write, copy, verify both exist with same content."""
        slim_fs.write("/local/src.txt", b"copy me")
        slim_fs.copy("/local/src.txt", "/local/dst.txt")
        src = slim_fs.read("/local/src.txt")
        dst = slim_fs.read("/local/dst.txt")
        assert src == dst == b"copy me"

    def test_mkdir(self, slim_fs: SlimNexusFS):
        """Create directory, verify it's a directory."""
        slim_fs.mkdir("/local/subdir")
        stat = slim_fs.stat("/local/subdir")
        assert stat is not None
        assert stat["is_directory"] is True

    def test_stat_directory(self, slim_fs: SlimNexusFS):
        """Stat on the mount root should return directory."""
        stat = slim_fs.stat("/local")
        assert stat is not None
        assert stat["is_directory"] is True

    def test_overwrite(self, slim_fs: SlimNexusFS):
        """Writing to the same path should overwrite."""
        slim_fs.write("/local/ow.txt", b"version 1")
        slim_fs.write("/local/ow.txt", b"version 2")
        result = slim_fs.read("/local/ow.txt")
        assert result == b"version 2"

    def test_binary_content(self, slim_fs: SlimNexusFS):
        """Write and read binary content."""
        content = bytes(range(256))
        slim_fs.write("/local/binary.bin", content)
        result = slim_fs.read("/local/binary.bin")
        assert result == content

    def test_empty_file(self, slim_fs: SlimNexusFS):
        """Write and read empty file."""
        slim_fs.write("/local/empty.txt", b"")
        result = slim_fs.read("/local/empty.txt")
        assert result == b""

    def test_list_mounts(self, slim_fs: SlimNexusFS):
        """Verify mount points are listed."""
        mounts = slim_fs.list_mounts()
        assert "/local" in mounts


# ---------------------------------------------------------------------------
# Edit operations
# ---------------------------------------------------------------------------


class TestEditOperations:
    """Test the edit() method on SlimNexusFS facade."""

    def test_edit_simple_replacement(self, slim_fs: SlimNexusFS):
        """Simple search/replace edit."""
        slim_fs.write("/local/code.py", b"def foo():\n    return 1\n")

        result = slim_fs.edit("/local/code.py", [("def foo():", "def bar():")])

        assert result["success"] is True
        assert result["applied_count"] == 1
        content = slim_fs.read("/local/code.py")
        assert b"def bar():" in content
        assert b"def foo():" not in content

    def test_edit_multiple_replacements(self, slim_fs: SlimNexusFS):
        """Multiple edits applied in sequence."""
        slim_fs.write("/local/multi.py", b"x = 1\ny = 2\nz = 3\n")

        result = slim_fs.edit(
            "/local/multi.py",
            [("x = 1", "x = 10"), ("y = 2", "y = 20")],
        )

        assert result["success"] is True
        assert result["applied_count"] == 2
        content = slim_fs.read("/local/multi.py")
        assert content == b"x = 10\ny = 20\nz = 3\n"

    def test_edit_returns_diff(self, slim_fs: SlimNexusFS):
        """Edit result includes a unified diff."""
        slim_fs.write("/local/diff.txt", b"hello world\n")

        result = slim_fs.edit("/local/diff.txt", [("hello", "goodbye")])

        assert result["success"] is True
        assert "-hello world" in result["diff"]
        assert "+goodbye world" in result["diff"]

    def test_edit_preview_does_not_modify(self, slim_fs: SlimNexusFS):
        """Preview mode returns diff but doesn't write."""
        original = b"keep me unchanged\n"
        slim_fs.write("/local/preview.txt", original)

        result = slim_fs.edit(
            "/local/preview.txt",
            [("keep me unchanged", "I was changed")],
            preview=True,
        )

        assert result["success"] is True
        assert "+I was changed" in result["diff"]
        # File should NOT have changed
        content = slim_fs.read("/local/preview.txt")
        assert content == original

    def test_edit_no_match_fails(self, slim_fs: SlimNexusFS):
        """Edit fails when search string not found."""
        slim_fs.write("/local/nomatch.txt", b"actual content\n")

        result = slim_fs.edit(
            "/local/nomatch.txt",
            [("nonexistent text", "replacement")],
            fuzzy_threshold=1.0,
        )

        assert result["success"] is False
        assert len(result["errors"]) > 0

    def test_edit_with_dict_format(self, slim_fs: SlimNexusFS):
        """Edit accepts dict format with old_str/new_str keys."""
        slim_fs.write("/local/dict.txt", b"old value\n")

        result = slim_fs.edit(
            "/local/dict.txt",
            [{"old_str": "old value", "new_str": "new value"}],
        )

        assert result["success"] is True
        content = slim_fs.read("/local/dict.txt")
        assert content == b"new value\n"

    def test_edit_fuzzy_match(self, slim_fs: SlimNexusFS):
        """Fuzzy matching handles minor differences."""
        slim_fs.write(
            "/local/fuzzy.py",
            b"def calculate_total(items):\n    return sum(items)\n",
        )

        result = slim_fs.edit(
            "/local/fuzzy.py",
            [("def calcuate_total(items):", "def compute_sum(items):")],
            fuzzy_threshold=0.8,
        )

        assert result["success"] is True
        assert result["matches"][0]["match_type"] == "fuzzy"
        content = slim_fs.read("/local/fuzzy.py")
        assert b"def compute_sum(items):" in content

    def test_edit_etag_concurrency(self, slim_fs: SlimNexusFS):
        """Optimistic concurrency: edit with correct etag succeeds."""
        write_result = slim_fs.write("/local/etag.txt", b"version 1\n")
        etag = write_result["etag"]

        result = slim_fs.edit(
            "/local/etag.txt",
            [("version 1", "version 2")],
            if_match=etag,
        )

        assert result["success"] is True
        content = slim_fs.read("/local/etag.txt")
        assert content == b"version 2\n"

    def test_edit_stale_etag_fails(self, slim_fs: SlimNexusFS):
        """Optimistic concurrency: stale etag is rejected."""
        write_result = slim_fs.write("/local/stale.txt", b"version 1\n")
        old_etag = write_result["etag"]

        # Overwrite to change the etag
        slim_fs.write("/local/stale.txt", b"version 2\n")

        from nexus.contracts.exceptions import ConflictError

        with pytest.raises(ConflictError):
            slim_fs.edit(
                "/local/stale.txt",
                [("version 2", "version 3")],
                if_match=old_etag,
            )

    def test_edit_delete_text(self, slim_fs: SlimNexusFS):
        """Replace with empty string to delete text."""
        slim_fs.write("/local/del.txt", b"keep\nremove me\nkeep too\n")

        result = slim_fs.edit("/local/del.txt", [("remove me\n", "")])

        assert result["success"] is True
        content = slim_fs.read("/local/del.txt")
        assert content == b"keep\nkeep too\n"

    def test_edit_multiline_block(self, slim_fs: SlimNexusFS):
        """Edit a multiline block."""
        slim_fs.write(
            "/local/block.py",
            b"def old():\n    pass\n\ndef other():\n    pass\n",
        )

        result = slim_fs.edit(
            "/local/block.py",
            [("def old():\n    pass", "def new():\n    return 42")],
        )

        assert result["success"] is True
        content = slim_fs.read("/local/block.py")
        assert b"def new():\n    return 42" in content
        assert b"def other():\n    pass" in content


# ---------------------------------------------------------------------------
# Multi-backend
# ---------------------------------------------------------------------------


class TestMultiBackend:
    def test_write_to_separate_backends(self, dual_fs: SlimNexusFS):
        """Write to two different backends, verify isolation."""
        dual_fs.write("/a/file.txt", b"backend A")
        dual_fs.write("/b/file.txt", b"backend B")

        assert dual_fs.read("/a/file.txt") == b"backend A"
        assert dual_fs.read("/b/file.txt") == b"backend B"

    def test_cross_backend_copy(self, dual_fs: SlimNexusFS):
        """Copy from one backend to another."""
        dual_fs.write("/a/src.txt", b"cross-copy")
        dual_fs.copy("/a/src.txt", "/b/dst.txt")

        assert dual_fs.read("/b/dst.txt") == b"cross-copy"

    def test_list_multiple_mounts(self, dual_fs: SlimNexusFS):
        """Both mounts should be visible."""
        mounts = dual_fs.list_mounts()
        assert "/a" in mounts
        assert "/b" in mounts


# ---------------------------------------------------------------------------
# SQLite metastore
# ---------------------------------------------------------------------------


class TestSQLiteMetastore:
    def test_wal_mode_enabled(self, tmp_path: Path):
        """Verify WAL mode is enabled on the SQLite database."""
        db_path = str(tmp_path / "test.db")
        SQLiteMetastore(db_path)  # creates DB with WAL mode
        import sqlite3

        conn = sqlite3.connect(db_path)
        result = conn.execute("PRAGMA journal_mode").fetchone()
        assert result[0] == "wal"
        conn.close()

    def test_put_and_get(self, tmp_path: Path):
        """Basic put/get on the SQLite metastore."""
        from datetime import UTC, datetime

        from nexus.contracts.metadata import FileMetadata

        db_path = str(tmp_path / "test.db")
        meta = SQLiteMetastore(db_path)

        fm = FileMetadata(
            path="/test/file.txt",
            backend_name="local",
            physical_path="abc123",
            size=42,
            etag="abc123",
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
