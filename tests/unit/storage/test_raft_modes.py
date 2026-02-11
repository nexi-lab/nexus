"""Tests for RaftMetadataStore mode-aware operations (Issue #1180).

Tests the StoreMode integration with RaftMetadataStore: constructor mode
parameter, _is_local property, _get_local / _put_local / _delete_local /
_list_local private helpers, public get/put delegation, and
get_replication_status() stub.

These tests are written RED-first.  The constructor and private methods
tested here do not yet exist -- they will be added in the implementation
phase of Issue #1180.

Mocking approach:
    PyO3 classes (Metastore, RaftConsensus, RaftClient) are NOT imported.
    Instead we inject a lightweight FakeSledStore into local_raft so the
    store can operate on an in-memory dict.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.core._metadata_generated import FileMetadata
from nexus.core.consistency import StoreMode

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeSledStore:
    """In-memory stand-in for the Rust Metastore / RaftConsensus PyO3 object.

    Provides the same method signatures that RaftMetadataStore._local calls:
        get_metadata(path) -> bytes | None
        set_metadata(path, data: bytes)
        delete_metadata(path)
        list_metadata(prefix) -> list[tuple[str, bytes]]
    """

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    def get_metadata(self, path: str) -> bytes | None:
        return self._data.get(path)

    def set_metadata(self, path: str, data: bytes) -> None:
        self._data[path] = data

    def delete_metadata(self, path: str) -> None:
        self._data.pop(path, None)

    def list_metadata(self, prefix: str) -> list[tuple[str, bytes]]:
        return [(k, v) for k, v in self._data.items() if k.startswith(prefix)]

    def flush(self) -> None:
        pass


def _make_metadata(
    path: str = "/test/file.txt",
    backend_name: str = "local",
    physical_path: str = "/data/abc123",
    size: int = 1024,
    **kwargs: Any,
) -> FileMetadata:
    """Create a FileMetadata instance with sensible defaults."""
    return FileMetadata(
        path=path,
        backend_name=backend_name,
        physical_path=physical_path,
        size=size,
        **kwargs,
    )


def _build_store(
    mode: StoreMode,
    sled: FakeSledStore | None = None,
    zone_id: str | None = None,
) -> Any:
    """Construct a RaftMetadataStore with the given mode and fake sled backend.

    Import is deferred so the module-level import of PyO3 does not fail
    in test environments where Rust extensions are unavailable.
    """
    from nexus.storage.raft_metadata_store import RaftMetadataStore

    if sled is None:
        sled = FakeSledStore()

    if mode == StoreMode.REMOTE:
        remote = MagicMock()
        return RaftMetadataStore(
            remote_client=remote,
            zone_id=zone_id,
            mode=mode,
        )

    return RaftMetadataStore(
        local_raft=sled,
        zone_id=zone_id,
        mode=mode,
    )


# ---------------------------------------------------------------------------
# 1-4: Constructor sets _mode correctly for each StoreMode variant
# ---------------------------------------------------------------------------


class TestStoreModeConstructor:
    """Verify that the mode parameter is persisted on the instance."""

    def test_embedded_mode_set(self) -> None:
        """Constructor with mode=EMBEDDED stores StoreMode.EMBEDDED."""
        store = _build_store(StoreMode.EMBEDDED)
        assert store._mode == StoreMode.EMBEDDED

    def test_sc_mode_set(self) -> None:
        """Constructor with mode=SC stores StoreMode.SC."""
        store = _build_store(StoreMode.SC)
        assert store._mode == StoreMode.SC

    def test_ec_mode_set(self) -> None:
        """Constructor with mode=EC stores StoreMode.EC."""
        store = _build_store(StoreMode.EC)
        assert store._mode == StoreMode.EC

    def test_remote_mode_set(self) -> None:
        """Constructor with mode=REMOTE stores StoreMode.REMOTE."""
        store = _build_store(StoreMode.REMOTE)
        assert store._mode == StoreMode.REMOTE


# ---------------------------------------------------------------------------
# 5-8: _is_local property reflects mode correctly
# ---------------------------------------------------------------------------


class TestIsLocalProperty:
    """_is_local should be True for EMBEDDED/SC/EC and False for REMOTE."""

    def test_is_local_true_for_embedded(self) -> None:
        store = _build_store(StoreMode.EMBEDDED)
        assert store._is_local is True

    def test_is_local_true_for_sc(self) -> None:
        store = _build_store(StoreMode.SC)
        assert store._is_local is True

    def test_is_local_true_for_ec(self) -> None:
        store = _build_store(StoreMode.EC)
        assert store._is_local is True

    def test_is_local_false_for_remote(self) -> None:
        store = _build_store(StoreMode.REMOTE)
        assert store._is_local is False


# ---------------------------------------------------------------------------
# 9-12: _get_local / _put_local / _delete_local private helpers
# ---------------------------------------------------------------------------


class TestLocalHelpers:
    """Test the thin wrappers over the sled backend."""

    def test_get_local_returns_metadata(self) -> None:
        """_get_local returns deserialized FileMetadata when data exists."""
        sled = FakeSledStore()
        store = _build_store(StoreMode.EMBEDDED, sled=sled)

        # Seed the sled store with serialized metadata
        from nexus.storage.raft_metadata_store import _serialize_metadata

        meta = _make_metadata(path="/docs/readme.md", size=512)
        sled.set_metadata("/docs/readme.md", _serialize_metadata(meta))

        result = store._get_local("/docs/readme.md")
        assert result is not None
        assert result.path == "/docs/readme.md"
        assert result.size == 512

    def test_get_local_returns_none(self) -> None:
        """_get_local returns None when no data exists for the path."""
        store = _build_store(StoreMode.EMBEDDED)
        result = store._get_local("/nonexistent")
        assert result is None

    def test_put_local_stores_data(self) -> None:
        """_put_local writes serialized metadata into the sled backend."""
        sled = FakeSledStore()
        store = _build_store(StoreMode.EMBEDDED, sled=sled)

        meta = _make_metadata(path="/data/report.csv", size=2048)
        store._put_local(meta)

        # Verify sled received the data
        raw = sled.get_metadata("/data/report.csv")
        assert raw is not None
        assert len(raw) > 0

    def test_delete_local_removes_data(self) -> None:
        """_delete_local removes the entry from the sled backend."""
        sled = FakeSledStore()
        store = _build_store(StoreMode.EMBEDDED, sled=sled)

        # Seed data then delete
        from nexus.storage.raft_metadata_store import _serialize_metadata

        meta = _make_metadata(path="/tmp/scratch.txt")
        sled.set_metadata("/tmp/scratch.txt", _serialize_metadata(meta))

        store._delete_local("/tmp/scratch.txt")
        assert sled.get_metadata("/tmp/scratch.txt") is None


# ---------------------------------------------------------------------------
# 13-14: _list_local filters meta: keys and handles recursive flag
# ---------------------------------------------------------------------------


class TestListLocal:
    """Test _list_local prefix filtering and recursive logic."""

    def test_list_local_filters_meta_keys(self) -> None:
        """_list_local skips entries whose key starts with 'meta:'."""
        sled = FakeSledStore()
        store = _build_store(StoreMode.EMBEDDED, sled=sled)

        from nexus.storage.raft_metadata_store import _serialize_metadata

        # Two real file entries
        file_a = _make_metadata(path="/data/a.txt", size=100)
        file_b = _make_metadata(path="/data/b.txt", size=200)
        sled.set_metadata("/data/a.txt", _serialize_metadata(file_a))
        sled.set_metadata("/data/b.txt", _serialize_metadata(file_b))

        # One extended-attribute entry that should be skipped
        sled.set_metadata(
            "meta:/data/a.txt:parsed_text",
            json.dumps("hello world").encode("utf-8"),
        )

        results = store._list_local("/data/", recursive=True)
        paths = [m.path for m in results]
        assert "/data/a.txt" in paths
        assert "/data/b.txt" in paths
        assert len(results) == 2  # meta: entry excluded

    def test_list_local_non_recursive(self) -> None:
        """_list_local with recursive=False returns only direct children."""
        sled = FakeSledStore()
        store = _build_store(StoreMode.EMBEDDED, sled=sled)

        from nexus.storage.raft_metadata_store import _serialize_metadata

        direct_child = _make_metadata(path="/data/top.txt", size=10)
        nested_child = _make_metadata(path="/data/sub/deep.txt", size=20)
        sled.set_metadata("/data/top.txt", _serialize_metadata(direct_child))
        sled.set_metadata("/data/sub/deep.txt", _serialize_metadata(nested_child))

        results = store._list_local("/data/", recursive=False)
        paths = [m.path for m in results]
        assert "/data/top.txt" in paths
        # Nested path should be excluded when non-recursive
        assert "/data/sub/deep.txt" not in paths


# ---------------------------------------------------------------------------
# 15-16: Public get/put delegate to _get_local / _put_local for local modes
# ---------------------------------------------------------------------------


class TestPublicDelegation:
    """Sync get() and put() should delegate to _*_local helpers in local mode."""

    def test_get_delegates_to_get_local(self) -> None:
        """get() for a local-mode store calls _get_local."""
        sled = FakeSledStore()
        store = _build_store(StoreMode.SC, sled=sled)

        from nexus.storage.raft_metadata_store import _serialize_metadata

        meta = _make_metadata(path="/files/test.py", size=777)
        sled.set_metadata("/files/test.py", _serialize_metadata(meta))

        result = store.get("/files/test.py")
        assert result is not None
        assert result.path == "/files/test.py"
        assert result.size == 777

    def test_put_delegates_to_put_local(self) -> None:
        """put() for a local-mode store calls _put_local."""
        sled = FakeSledStore()
        store = _build_store(StoreMode.SC, sled=sled)

        meta = _make_metadata(path="/files/output.log", size=333)
        store.put(meta)

        # Verify data landed in the sled backend
        raw = sled.get_metadata("/files/output.log")
        assert raw is not None


# ---------------------------------------------------------------------------
# 17-18: get_replication_status()
# ---------------------------------------------------------------------------


class TestReplicationStatus:
    """get_replication_status() returns a status dict or raises for EC mode."""

    def test_replication_status_non_ec(self) -> None:
        """Non-EC modes return a dict with mode, lag=0, uncommitted=0."""
        for mode in (StoreMode.EMBEDDED, StoreMode.SC, StoreMode.REMOTE):
            store = _build_store(mode)
            status = store.get_replication_status()
            assert status == {
                "mode": mode.value,
                "lag": 0,
                "uncommitted": 0,
            }

    def test_replication_status_ec_raises(self) -> None:
        """EC mode raises NotImplementedError (requires async lag tracking)."""
        store = _build_store(StoreMode.EC)
        with pytest.raises(NotImplementedError):
            store.get_replication_status()
