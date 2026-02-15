"""Integration tests for RaftMetadataStore with mocked PyO3 bridge.

Tests the full Python layer: CRUD, locks, batch ops, custom file metadata,
and searchable text — without requiring the compiled Rust PyO3 library.
A FakeLocalRaft simulates the real PyO3 LocalRaft behavior in pure Python.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest

from nexus.core._metadata_generated import FileMetadata, PaginatedResult
from nexus.storage.raft_metadata_store import RaftMetadataStore

# ---------------------------------------------------------------------------
# FakeLocalRaft: pure-Python stand-in for the Rust PyO3 LocalRaft
# ---------------------------------------------------------------------------


@dataclass
class FakeHolderInfo:
    lock_id: str
    holder_info: str
    acquired_at: int
    expires_at: int


@dataclass
class FakeLockState:
    acquired: bool
    current_holders: int
    max_holders: int
    holders: list[FakeHolderInfo] = field(default_factory=list)


@dataclass
class FakeLockInfo:
    path: str
    max_holders: int
    holders: list[FakeHolderInfo] = field(default_factory=list)


class FakeLocalRaft:
    """Pure-Python fake that mirrors the PyO3 LocalRaft API."""

    def __init__(self) -> None:
        self._metadata: dict[str, bytes] = {}
        self._locks: dict[str, FakeLockInfo] = {}

    # -- Metadata ----------------------------------------------------------

    def set_metadata(self, path: str, value: bytes | list[int], *, consistency: str = "sc") -> bool:
        if isinstance(value, list):
            value = bytes(value)
        self._metadata[path] = value
        return True

    def get_metadata(self, path: str) -> bytes | None:
        return self._metadata.get(path)

    def delete_metadata(self, path: str, *, consistency: str = "sc") -> bool:
        return self._metadata.pop(path, None) is not None

    def list_metadata(self, prefix: str) -> list[tuple[str, bytes]]:
        return sorted([(k, v) for k, v in self._metadata.items() if k.startswith(prefix)])

    def get_metadata_multi(self, keys: list[str]) -> list[tuple[str, bytes | None]]:
        return [(k, self._metadata.get(k)) for k in keys]

    def list_metadata_paginated(
        self, prefix: str, cursor: str = "", limit: int = 100
    ) -> tuple[list[tuple[str, bytes]], str]:
        items = sorted(
            [(k, v) for k, v in self._metadata.items() if k.startswith(prefix) and k > cursor]
        )
        page = items[:limit]
        next_cursor = page[-1][0] if len(page) == limit else ""
        return page, next_cursor

    def batch_set_metadata(self, items: list[tuple[str, bytes]]) -> int:
        for path, value in items:
            self.set_metadata(path, value)
        return len(items)

    def batch_delete_metadata(self, keys: list[str]) -> int:
        for key in keys:
            self.delete_metadata(key)
        return len(keys)

    def count_metadata(self, prefix: str) -> int:
        return len(self.list_metadata(prefix))

    # -- Locks -------------------------------------------------------------

    def acquire_lock(
        self,
        path: str,
        lock_id: str,
        max_holders: int = 1,
        ttl_secs: int = 30,
        holder_info: str = "",
    ) -> FakeLockState:
        now = int(time.time())
        expires_at = now + ttl_secs

        if path not in self._locks:
            self._locks[path] = FakeLockInfo(path=path, max_holders=max_holders, holders=[])

        lock = self._locks[path]
        # Expire old holders
        lock.holders = [h for h in lock.holders if h.expires_at > now]

        if len(lock.holders) < lock.max_holders:
            holder = FakeHolderInfo(
                lock_id=lock_id,
                holder_info=holder_info,
                acquired_at=now,
                expires_at=expires_at,
            )
            lock.holders.append(holder)
            return FakeLockState(
                acquired=True,
                current_holders=len(lock.holders),
                max_holders=lock.max_holders,
                holders=list(lock.holders),
            )
        return FakeLockState(
            acquired=False,
            current_holders=len(lock.holders),
            max_holders=lock.max_holders,
            holders=list(lock.holders),
        )

    def release_lock(self, path: str, lock_id: str) -> bool:
        if path not in self._locks:
            return False
        lock = self._locks[path]
        before = len(lock.holders)
        lock.holders = [h for h in lock.holders if h.lock_id != lock_id]
        return len(lock.holders) < before

    def extend_lock(self, path: str, lock_id: str, new_ttl_secs: int) -> bool:
        if path not in self._locks:
            return False
        for h in self._locks[path].holders:
            if h.lock_id == lock_id:
                h.expires_at = int(time.time()) + new_ttl_secs
                return True
        return False

    def get_lock(self, path: str) -> FakeLockInfo | None:
        return self._locks.get(path)

    def list_locks(self, prefix: str = "", limit: int = 1000) -> list[FakeLockInfo]:
        matches = [v for k, v in self._locks.items() if k.startswith(prefix)]
        return matches[:limit]

    def force_release_lock(self, path: str) -> bool:
        if path in self._locks and self._locks[path].holders:
            self._locks[path].holders = []
            return True
        return False

    def flush(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(fake: FakeLocalRaft | None = None) -> RaftMetadataStore:
    """Create a RaftMetadataStore backed by a FakeLocalRaft."""
    fake = fake or FakeLocalRaft()
    store = object.__new__(RaftMetadataStore)
    store._engine = fake
    store._client = None
    store._zone_id = None
    return store


def _make_meta(
    path: str = "/test/file.txt",
    backend_name: str = "local",
    physical_path: str = "/data/abc123",
    size: int = 1024,
    **kwargs: Any,
) -> FileMetadata:
    return FileMetadata(
        path=path,
        backend_name=backend_name,
        physical_path=physical_path,
        size=size,
        **kwargs,
    )


# ===========================================================================
# CRUD Tests
# ===========================================================================


class TestCRUD:
    """Tests for get / put / delete / exists / rename_path."""

    def test_put_and_get(self) -> None:
        store = _make_store()
        meta = _make_meta(path="/docs/readme.md", size=512, etag="abc")
        store.put(meta)

        result = store.get("/docs/readme.md")
        assert result is not None
        assert result.path == "/docs/readme.md"
        assert result.size == 512
        assert result.etag == "abc"

    def test_get_nonexistent_returns_none(self) -> None:
        store = _make_store()
        assert store.get("/no/such/file") is None

    def test_exists(self) -> None:
        store = _make_store()
        store.put(_make_meta(path="/a.txt"))
        assert store.exists("/a.txt") is True
        assert store.exists("/b.txt") is False

    def test_delete_returns_info(self) -> None:
        store = _make_store()
        store.put(_make_meta(path="/del.txt", size=100, etag="e1"))

        info = store.delete("/del.txt")
        assert info is not None
        assert info["path"] == "/del.txt"
        assert info["size"] == 100
        assert info["etag"] == "e1"

        # Should be gone
        assert store.get("/del.txt") is None

    def test_delete_nonexistent_returns_none(self) -> None:
        store = _make_store()
        assert store.delete("/no/file") is None

    def test_rename_path(self) -> None:
        store = _make_store()
        store.put(_make_meta(path="/old.txt", size=42))

        store.rename_path("/old.txt", "/new.txt")

        assert store.get("/old.txt") is None
        result = store.get("/new.txt")
        assert result is not None
        assert result.path == "/new.txt"
        assert result.size == 42

    def test_rename_nonexistent_raises(self) -> None:
        store = _make_store()
        with pytest.raises(FileNotFoundError):
            store.rename_path("/nope", "/dest")

    def test_put_preserves_timestamps(self) -> None:
        store = _make_store()
        now = datetime(2026, 2, 10, 12, 0, 0)
        store.put(_make_meta(path="/ts.txt", created_at=now, modified_at=now))

        result = store.get("/ts.txt")
        assert result is not None
        assert result.created_at == now
        assert result.modified_at == now

    def test_put_overwrite(self) -> None:
        store = _make_store()
        store.put(_make_meta(path="/v.txt", size=1, version=1))
        store.put(_make_meta(path="/v.txt", size=2, version=2))

        result = store.get("/v.txt")
        assert result is not None
        assert result.size == 2
        assert result.version == 2


# ===========================================================================
# List and Pagination Tests
# ===========================================================================


class TestListOperations:
    """Tests for list, list_paginated, is_implicit_directory."""

    def _populate(self, store: RaftMetadataStore, paths: list[str]) -> None:
        for p in paths:
            store.put(_make_meta(path=p))

    def test_list_recursive(self) -> None:
        store = _make_store()
        self._populate(store, ["/a/1.txt", "/a/2.txt", "/a/sub/3.txt"])

        items = store.list(prefix="/a/", recursive=True)
        paths = [i.path for i in items]
        assert "/a/1.txt" in paths
        assert "/a/sub/3.txt" in paths

    def test_list_non_recursive(self) -> None:
        store = _make_store()
        self._populate(store, ["/a/1.txt", "/a/2.txt", "/a/sub/3.txt"])

        items = store.list(prefix="/a/", recursive=False)
        paths = [i.path for i in items]
        assert "/a/1.txt" in paths
        assert "/a/2.txt" in paths
        assert "/a/sub/3.txt" not in paths

    def test_list_skips_meta_keys(self) -> None:
        """Extended attribute keys (meta:...) should be excluded from list."""
        fake = FakeLocalRaft()
        store = _make_store(fake)
        store.put(_make_meta(path="/f/a.txt"))
        # Manually insert a meta key (as set_file_metadata would)
        fake.set_metadata(
            "meta:/f/a.txt:parsed_text",
            json.dumps("hello").encode(),
        )

        items = store.list(prefix="/f/")
        assert len(items) == 1
        assert items[0].path == "/f/a.txt"

    def test_is_implicit_directory(self) -> None:
        store = _make_store()
        store.put(_make_meta(path="/dir/file.txt"))
        assert store.is_implicit_directory("/dir") is True
        assert store.is_implicit_directory("/nonexistent") is False

    def test_list_paginated_basic(self) -> None:
        store = _make_store()
        self._populate(store, [f"/p/{chr(97 + i)}.txt" for i in range(5)])

        result = store.list_paginated(prefix="/p/", limit=3)
        assert isinstance(result, PaginatedResult)
        assert len(result.items) == 3
        assert result.has_more is True
        # total_count is None when unavailable without full scan
        assert result.total_count is None or result.total_count == 5

    def test_list_paginated_cursor(self) -> None:
        store = _make_store()
        self._populate(store, [f"/p/{chr(97 + i)}.txt" for i in range(5)])

        page1 = store.list_paginated(prefix="/p/", limit=2)
        assert len(page1.items) == 2
        assert page1.has_more is True

        page2 = store.list_paginated(prefix="/p/", limit=2, cursor=page1.next_cursor)
        assert len(page2.items) == 2
        # Pages should not overlap
        page1_paths = {i.path for i in page1.items}
        page2_paths = {i.path for i in page2.items}
        assert page1_paths.isdisjoint(page2_paths)


# ===========================================================================
# Batch Operations Tests
# ===========================================================================


class TestBatchOperations:
    """Tests for get_batch, put_batch, delete_batch, batch_get_content_ids."""

    def test_get_batch(self) -> None:
        store = _make_store()
        store.put(_make_meta(path="/b/1.txt", etag="e1"))
        store.put(_make_meta(path="/b/2.txt", etag="e2"))

        result = store.get_batch(["/b/1.txt", "/b/2.txt", "/b/missing.txt"])
        assert result["/b/1.txt"] is not None
        assert result["/b/1.txt"].etag == "e1"
        assert result["/b/2.txt"] is not None
        assert result["/b/missing.txt"] is None

    def test_put_batch(self) -> None:
        store = _make_store()
        items = [_make_meta(path=f"/batch/{i}.txt") for i in range(3)]
        store.put_batch(items)

        for i in range(3):
            assert store.exists(f"/batch/{i}.txt")

    def test_delete_batch(self) -> None:
        store = _make_store()
        for i in range(3):
            store.put(_make_meta(path=f"/del/{i}.txt"))

        store.delete_batch(["/del/0.txt", "/del/2.txt"])
        assert store.exists("/del/0.txt") is False
        assert store.exists("/del/1.txt") is True
        assert store.exists("/del/2.txt") is False

    def test_batch_get_content_ids(self) -> None:
        store = _make_store()
        store.put(_make_meta(path="/c/a.txt", etag="hash-a"))
        store.put(_make_meta(path="/c/b.txt", etag="hash-b"))

        result = store.batch_get_content_ids(["/c/a.txt", "/c/b.txt", "/c/none.txt"])
        assert result["/c/a.txt"] == "hash-a"
        assert result["/c/b.txt"] == "hash-b"
        assert result["/c/none.txt"] is None


# ===========================================================================
# Custom File Metadata Tests
# ===========================================================================


class TestCustomFileMetadata:
    """Tests for set_file_metadata, get_file_metadata, searchable text."""

    def test_set_and_get_custom_metadata(self) -> None:
        store = _make_store()
        store.put(_make_meta(path="/doc.txt"))
        store.set_file_metadata("/doc.txt", "parser_name", "tika")

        value = store.get_file_metadata("/doc.txt", "parser_name")
        assert value == "tika"

    def test_get_nonexistent_custom_metadata(self) -> None:
        store = _make_store()
        assert store.get_file_metadata("/no.txt", "key") is None

    def test_set_custom_metadata_to_none_deletes(self) -> None:
        store = _make_store()
        store.set_file_metadata("/doc.txt", "key", "value")
        assert store.get_file_metadata("/doc.txt", "key") == "value"

        store.set_file_metadata("/doc.txt", "key", None)
        assert store.get_file_metadata("/doc.txt", "key") is None

    def test_custom_metadata_complex_values(self) -> None:
        store = _make_store()
        store.set_file_metadata("/doc.txt", "tags", ["python", "raft"])
        assert store.get_file_metadata("/doc.txt", "tags") == ["python", "raft"]

        store.set_file_metadata("/doc.txt", "stats", {"lines": 42, "words": 100})
        assert store.get_file_metadata("/doc.txt", "stats") == {
            "lines": 42,
            "words": 100,
        }

    def test_searchable_text(self) -> None:
        store = _make_store()
        store.set_file_metadata("/doc.txt", "parsed_text", "Hello World")

        text = store.get_searchable_text("/doc.txt")
        assert text == "Hello World"

    def test_searchable_text_none_when_missing(self) -> None:
        store = _make_store()
        assert store.get_searchable_text("/no.txt") is None

    def test_searchable_text_bulk(self) -> None:
        store = _make_store()
        store.set_file_metadata("/a.txt", "parsed_text", "Content A")
        store.set_file_metadata("/b.txt", "parsed_text", "Content B")

        result = store.get_searchable_text_bulk(["/a.txt", "/b.txt", "/c.txt"])
        assert result == {"/a.txt": "Content A", "/b.txt": "Content B"}
        # /c.txt should not be in result (missing)
        assert "/c.txt" not in result

    def test_get_file_metadata_bulk(self) -> None:
        store = _make_store()
        store.set_file_metadata("/x.txt", "key", "val-x")
        store.set_file_metadata("/y.txt", "key", "val-y")

        result = store.get_file_metadata_bulk(["/x.txt", "/y.txt", "/z.txt"], "key")
        assert result["/x.txt"] == "val-x"
        assert result["/y.txt"] == "val-y"
        assert result["/z.txt"] is None


# ===========================================================================
# Lock Operation Tests
# ===========================================================================


class TestLockOperations:
    """Tests for lock acquire, release, extend, info, list, force-release."""

    def test_acquire_and_release(self) -> None:
        store = _make_store()
        acquired = store.acquire_lock("/res", "h1")
        assert acquired is True

        released = store.release_lock("/res", "h1")
        assert released is True

    def test_mutex_blocks_second_holder(self) -> None:
        store = _make_store()
        assert store.acquire_lock("/res", "h1", max_holders=1) is True
        assert store.acquire_lock("/res", "h2", max_holders=1) is False

    def test_semaphore_allows_multiple(self) -> None:
        store = _make_store()
        assert store.acquire_lock("/res", "h1", max_holders=3) is True
        assert store.acquire_lock("/res", "h2", max_holders=3) is True
        assert store.acquire_lock("/res", "h3", max_holders=3) is True
        # Fourth should fail
        assert store.acquire_lock("/res", "h4", max_holders=3) is False

    def test_release_unknown_returns_false(self) -> None:
        store = _make_store()
        assert store.release_lock("/res", "unknown") is False

    def test_extend_lock(self) -> None:
        store = _make_store()
        store.acquire_lock("/res", "h1", ttl_secs=10)
        extended = store.extend_lock("/res", "h1", ttl_secs=60)
        assert extended is True

    def test_extend_unknown_lock_returns_false(self) -> None:
        store = _make_store()
        assert store.extend_lock("/res", "unknown") is False

    def test_get_lock_info(self) -> None:
        store = _make_store()
        store.acquire_lock("/res", "h1")

        info = store.get_lock_info("/res")
        assert info is not None
        assert info["path"] == "/res"
        assert info["max_holders"] == 1
        assert len(info["holders"]) == 1
        assert info["holders"][0]["lock_id"] == "h1"

    def test_get_lock_info_nonexistent(self) -> None:
        store = _make_store()
        assert store.get_lock_info("/none") is None

    def test_list_locks(self) -> None:
        store = _make_store()
        store.acquire_lock("zone1:/a", "h1")
        store.acquire_lock("zone1:/b", "h2")
        store.acquire_lock("zone2:/c", "h3")

        zone1_locks = store.list_locks(prefix="zone1:")
        assert len(zone1_locks) == 2

        all_locks = store.list_locks()
        assert len(all_locks) == 3

    def test_force_release_lock(self) -> None:
        store = _make_store()
        store.acquire_lock("/res", "h1")
        store.acquire_lock("/res", "h2", max_holders=2)

        released = store.force_release_lock("/res")
        assert released is True

        # Lock should have no holders now
        info = store.get_lock_info("/res")
        assert info is None  # No holders means None

    def test_force_release_nonexistent(self) -> None:
        store = _make_store()
        assert store.force_release_lock("/none") is False

    def test_close_flushes(self) -> None:
        """close() should call flush() without error."""
        fake = FakeLocalRaft()
        store = _make_store(fake)
        store.close()  # Should not raise


# ===========================================================================
# SC/EC Consistency Hint Tests (#1364)
# ===========================================================================


class TestConsistencyHints:
    """Tests for per-operation SC/EC consistency parameter."""

    def test_put_sc_default(self) -> None:
        """Default put() uses SC (backward compatible)."""
        store = _make_store()
        meta = _make_meta(path="/sc/file.txt", size=100)
        store.put(meta)
        result = store.get("/sc/file.txt")
        assert result is not None
        assert result.size == 100

    def test_put_ec(self) -> None:
        """put() with consistency='ec' stores data."""
        store = _make_store()
        meta = _make_meta(path="/ec/file.txt", size=200)
        store.put(meta, consistency="ec")
        result = store.get("/ec/file.txt")
        assert result is not None
        assert result.size == 200

    def test_put_sc_explicit(self) -> None:
        """put() with explicit consistency='sc' works."""
        store = _make_store()
        meta = _make_meta(path="/sc2/file.txt", size=300)
        store.put(meta, consistency="sc")
        result = store.get("/sc2/file.txt")
        assert result is not None
        assert result.size == 300

    def test_delete_sc_default(self) -> None:
        """Default delete() uses SC."""
        store = _make_store()
        store.put(_make_meta(path="/del-sc.txt", size=10, etag="e"))
        info = store.delete("/del-sc.txt")
        assert info is not None
        assert info["path"] == "/del-sc.txt"
        assert store.get("/del-sc.txt") is None

    def test_delete_ec(self) -> None:
        """delete() with consistency='ec' removes data."""
        store = _make_store()
        store.put(_make_meta(path="/del-ec.txt", size=20, etag="e2"))
        info = store.delete("/del-ec.txt", consistency="ec")
        assert info is not None
        assert store.get("/del-ec.txt") is None

    def test_lock_operations_have_no_consistency_param(self) -> None:
        """Lock operations are always SC — no consistency parameter."""
        import inspect

        store = _make_store()
        # Verify acquire_lock, release_lock, extend_lock signatures
        # do NOT have a 'consistency' parameter
        for method_name in ("acquire_lock", "release_lock", "extend_lock"):
            sig = inspect.signature(getattr(store, method_name))
            assert "consistency" not in sig.parameters, (
                f"{method_name} should not have a consistency parameter"
            )

    def test_fake_set_metadata_accepts_consistency_kwarg(self) -> None:
        """FakeLocalRaft.set_metadata accepts consistency kwarg."""
        fake = FakeLocalRaft()
        fake.set_metadata("/test", b"data", consistency="ec")
        assert fake.get_metadata("/test") == b"data"
        fake.set_metadata("/test2", b"data2", consistency="sc")
        assert fake.get_metadata("/test2") == b"data2"

    def test_fake_delete_metadata_accepts_consistency_kwarg(self) -> None:
        """FakeLocalRaft.delete_metadata accepts consistency kwarg."""
        fake = FakeLocalRaft()
        fake.set_metadata("/test", b"data")
        assert fake.delete_metadata("/test", consistency="ec") is True
        assert fake.get_metadata("/test") is None
