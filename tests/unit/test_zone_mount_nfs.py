"""Tests for NFS-compliant zone mount/unmount behavior (Task #120).

Validates:
1. mount() requires mount point to exist as DT_DIR (strict Linux NFS)
2. mount() rejects non-existent path, non-directory, already-mounted
3. mount() replaces DT_DIR with DT_MOUNT (controlled shadow)
4. unmount() restores original DT_DIR (NFS: umount reveals original dir)
5. share_subtree() auto-creates DT_DIR for implicit directories before mount

Uses a pure-Python FakeStore (dict-backed) to test without PyO3.
"""

from __future__ import annotations

import pytest

from nexus.core._metadata_generated import DT_DIR, DT_MOUNT, DT_REG, FileMetadata
from nexus.raft.zone_manager import ZoneManager


class FakeStore:
    """Dict-backed metadata store implementing get/put/delete/list_iter."""

    def __init__(self) -> None:
        self._data: dict[str, FileMetadata] = {}

    def get(self, path: str) -> FileMetadata | None:
        return self._data.get(path)

    def put(self, entry: FileMetadata) -> None:
        self._data[entry.path] = entry

    def delete(self, path: str) -> FileMetadata | None:
        return self._data.pop(path, None)

    def list_iter(self, prefix: str = "", recursive: bool = False):
        for path, entry in sorted(self._data.items()):
            if path == prefix or path.startswith(prefix + "/"):
                yield entry

    def exists(self, path: str) -> bool:
        return path in self._data


class _FakeZoneManager(ZoneManager):
    """ZoneManager bypassing PyO3 __init__ for pure-Python testing."""

    def __init__(self) -> None:
        # Skip super().__init__ — no PyO3
        self._stores: dict[str, FakeStore] = {}
        self._node_id = 1
        self._root_zone_id = "root"
        self._py_mgr = None

    def add_store(self, zone_id: str) -> FakeStore:
        store = FakeStore()
        self._stores[zone_id] = store
        return store

    def get_store(self, zone_id: str):  # type: ignore[override]
        return self._stores.get(zone_id)


def _bootstrap(store: FakeStore, zone_id: str) -> None:
    """Write root "/" entry with i_links_count=1."""
    store.put(
        FileMetadata(
            path="/",
            backend_name="virtual",
            physical_path="",
            size=0,
            entry_type=DT_DIR,
            zone_id=zone_id,
            i_links_count=1,
        )
    )


@pytest.fixture()
def mgr():
    """ZoneManager with root + target zones, both bootstrapped."""
    m = _FakeZoneManager()
    root = m.add_store("root")
    target = m.add_store("target")
    _bootstrap(root, "root")
    _bootstrap(target, "target")
    return m


# ---------------------------------------------------------------------------
# mount() requires DT_DIR (NFS compliance)
# ---------------------------------------------------------------------------


class TestMountRequiresDTDIR:
    def test_mount_on_existing_dir_succeeds(self, mgr):
        root = mgr.get_store("root")
        root.put(
            FileMetadata(
                path="/shared",
                backend_name="virtual",
                physical_path="",
                size=0,
                entry_type=DT_DIR,
                zone_id="root",
            )
        )

        mgr.mount("root", "/shared", "target")

        entry = root.get("/shared")
        assert entry is not None
        assert entry.entry_type == DT_MOUNT
        assert entry.target_zone_id == "target"

    def test_mount_on_nonexistent_path_fails(self, mgr):
        with pytest.raises(ValueError, match="does not exist"):
            mgr.mount("root", "/nonexistent", "target")

    def test_mount_on_regular_file_fails(self, mgr):
        root = mgr.get_store("root")
        root.put(
            FileMetadata(
                path="/file.txt",
                backend_name="local",
                physical_path="/data/file.txt",
                size=100,
                entry_type=DT_REG,
                zone_id="root",
            )
        )

        with pytest.raises(ValueError, match="not a directory"):
            mgr.mount("root", "/file.txt", "target")

    def test_mount_on_existing_mount_fails(self, mgr):
        root = mgr.get_store("root")
        root.put(
            FileMetadata(
                path="/shared",
                backend_name="virtual",
                physical_path="",
                size=0,
                entry_type=DT_DIR,
                zone_id="root",
            )
        )
        mgr.mount("root", "/shared", "target")

        with pytest.raises(ValueError, match="already a DT_MOUNT"):
            mgr.mount("root", "/shared", "target")


# ---------------------------------------------------------------------------
# mount() replaces DT_DIR with DT_MOUNT (controlled shadow)
# ---------------------------------------------------------------------------


class TestMountShadow:
    def test_mount_replaces_dir_with_mount_entry(self, mgr):
        root = mgr.get_store("root")
        root.put(
            FileMetadata(
                path="/mnt",
                backend_name="virtual",
                physical_path="",
                size=0,
                entry_type=DT_DIR,
                zone_id="root",
            )
        )

        assert root.get("/mnt").entry_type == DT_DIR
        mgr.mount("root", "/mnt", "target")

        entry = root.get("/mnt")
        assert entry.entry_type == DT_MOUNT
        assert entry.target_zone_id == "target"
        assert entry.backend_name == "mount"

    def test_mount_increments_target_links(self, mgr):
        root = mgr.get_store("root")
        target = mgr.get_store("target")
        root.put(
            FileMetadata(
                path="/shared",
                backend_name="virtual",
                physical_path="",
                size=0,
                entry_type=DT_DIR,
                zone_id="root",
            )
        )

        before = target.get("/").i_links_count
        mgr.mount("root", "/shared", "target")
        after = target.get("/").i_links_count
        assert after == before + 1


# ---------------------------------------------------------------------------
# mount() error cases
# ---------------------------------------------------------------------------


class TestMountErrors:
    def test_parent_zone_not_found(self, mgr):
        with pytest.raises(RuntimeError, match="Parent zone.*not found"):
            mgr.mount("nonexistent", "/shared", "target")

    def test_target_zone_not_found(self, mgr):
        root = mgr.get_store("root")
        root.put(
            FileMetadata(
                path="/shared",
                backend_name="virtual",
                physical_path="",
                size=0,
                entry_type=DT_DIR,
                zone_id="root",
            )
        )

        with pytest.raises(RuntimeError, match="Target zone.*not found"):
            mgr.mount("root", "/shared", "nonexistent")


# ---------------------------------------------------------------------------
# unmount() restores DT_DIR (NFS: umount reveals original directory)
# ---------------------------------------------------------------------------


class TestUnmountRestoresDTDIR:
    def test_unmount_restores_dir_entry(self, mgr):
        root = mgr.get_store("root")
        root.put(
            FileMetadata(
                path="/shared",
                backend_name="virtual",
                physical_path="",
                size=0,
                entry_type=DT_DIR,
                zone_id="root",
            )
        )

        mgr.mount("root", "/shared", "target")
        assert root.get("/shared").entry_type == DT_MOUNT

        mgr.unmount("root", "/shared")

        restored = root.get("/shared")
        assert restored is not None
        assert restored.entry_type == DT_DIR
        assert restored.zone_id == "root"

    def test_unmount_decrements_links(self, mgr):
        root = mgr.get_store("root")
        target = mgr.get_store("target")
        root.put(
            FileMetadata(
                path="/shared",
                backend_name="virtual",
                physical_path="",
                size=0,
                entry_type=DT_DIR,
                zone_id="root",
            )
        )

        mgr.mount("root", "/shared", "target")
        after_mount = target.get("/").i_links_count

        mgr.unmount("root", "/shared")
        after_unmount = target.get("/").i_links_count
        assert after_unmount == after_mount - 1

    def test_unmount_reveals_shadowed_entries(self, mgr):
        """After unmount, entries shadowed by DT_MOUNT become visible."""
        root = mgr.get_store("root")
        root.put(
            FileMetadata(
                path="/project",
                backend_name="virtual",
                physical_path="",
                size=0,
                entry_type=DT_DIR,
                zone_id="root",
            )
        )
        root.put(
            FileMetadata(
                path="/project/old.txt",
                backend_name="local",
                physical_path="/data/old.txt",
                size=42,
                entry_type=DT_REG,
                zone_id="root",
            )
        )

        mgr.mount("root", "/project", "target")

        # Shadowed entry still in store (not deleted by mount)
        assert root.get("/project/old.txt") is not None

        mgr.unmount("root", "/project")

        # After unmount, DT_DIR restored + old entries still there
        assert root.get("/project").entry_type == DT_DIR
        assert root.get("/project/old.txt").size == 42

    def test_unmount_allows_remount(self, mgr):
        """After unmount, the restored DT_DIR can be re-mounted."""
        root = mgr.get_store("root")
        root.put(
            FileMetadata(
                path="/shared",
                backend_name="virtual",
                physical_path="",
                size=0,
                entry_type=DT_DIR,
                zone_id="root",
            )
        )

        mgr.mount("root", "/shared", "target")
        mgr.unmount("root", "/shared")
        assert root.get("/shared").entry_type == DT_DIR

        # Re-mount should succeed
        mgr.mount("root", "/shared", "target")
        assert root.get("/shared").entry_type == DT_MOUNT


# ---------------------------------------------------------------------------
# share_subtree() ensures DT_DIR before mount
# ---------------------------------------------------------------------------


class TestShareSubtreeNFSCompliance:
    def test_share_implicit_dir_creates_dt_dir_for_mount(self, mgr):
        """share_subtree() on implicit dir creates DT_DIR before mount()."""
        root = mgr.get_store("root")

        # Files under /project/ but no explicit DT_DIR at /project
        root.put(
            FileMetadata(
                path="/project/main.py",
                backend_name="local",
                physical_path="/data/main.py",
                size=100,
                entry_type=DT_REG,
                zone_id="root",
            )
        )
        root.put(
            FileMetadata(
                path="/project/readme.md",
                backend_name="local",
                physical_path="/data/readme.md",
                size=50,
                entry_type=DT_REG,
                zone_id="root",
            )
        )
        assert root.get("/project") is None  # implicit dir

        new_store = mgr.add_store("new-zone")
        mgr.create_zone = lambda zid, peers=None: mgr.get_store(zid)

        zone_id = mgr.share_subtree("root", "/project", zone_id="new-zone")

        assert zone_id == "new-zone"
        entry = root.get("/project")
        assert entry is not None
        assert entry.entry_type == DT_MOUNT
        assert entry.target_zone_id == "new-zone"

    def test_share_explicit_dir_works(self, mgr):
        """share_subtree() on explicit DT_DIR works directly."""
        root = mgr.get_store("root")
        root.put(
            FileMetadata(
                path="/docs",
                backend_name="virtual",
                physical_path="",
                size=0,
                entry_type=DT_DIR,
                zone_id="root",
            )
        )
        root.put(
            FileMetadata(
                path="/docs/guide.md",
                backend_name="local",
                physical_path="/data/guide.md",
                size=200,
                entry_type=DT_REG,
                zone_id="root",
            )
        )

        mgr.add_store("docs-zone")
        mgr.create_zone = lambda zid, peers=None: mgr.get_store(zid)

        zone_id = mgr.share_subtree("root", "/docs", zone_id="docs-zone")

        assert zone_id == "docs-zone"
        assert root.get("/docs").entry_type == DT_MOUNT

        # New zone has rebased files
        new_store = mgr.get_store("docs-zone")
        assert new_store.get("/") is not None
        assert new_store.get("/guide.md") is not None

    def test_share_already_mounted_fails(self, mgr):
        root = mgr.get_store("root")
        root.put(
            FileMetadata(
                path="/shared",
                backend_name="mount",
                physical_path="",
                size=0,
                entry_type=DT_MOUNT,
                target_zone_id="target",
                zone_id="root",
            )
        )

        with pytest.raises(ValueError, match="already a DT_MOUNT"):
            mgr.share_subtree("root", "/shared", zone_id="new")
