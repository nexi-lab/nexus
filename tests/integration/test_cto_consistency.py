"""Integration tests for Close-to-Open Consistency Model (Issue #923).

Tests the full CTO consistency flow with real NexusFS instances:
- Write returns zookies, read with zookie sees fresh data
- Consistency levels (EVENTUAL, CLOSE_TO_OPEN, STRONG) behave correctly
- Edge cases: stale zookies, future zookies, invalid zookies, zone mismatches
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import Any

import pytest

from nexus.backends.local import LocalBackend
from nexus.core._metadata_generated import FileMetadata, FileMetadataProtocol
from nexus.core.consistency import FSConsistency
from nexus.core.nexus_fs import NexusFS
from nexus.core.permissions import OperationContext
from nexus.core.zookie import ConsistencyTimeoutError, InvalidZookieError, Zookie


class InMemoryFileMetadataStore(FileMetadataProtocol):
    """In-memory metadata store for tests that don't need Rust Raft extension."""

    def __init__(self) -> None:
        self._store: dict[str, FileMetadata] = {}

    def get(self, path: str) -> FileMetadata | None:
        return self._store.get(path)

    def put(self, metadata: FileMetadata) -> None:
        self._store[metadata.path] = metadata

    def delete(self, path: str) -> dict[str, Any] | None:
        if path in self._store:
            del self._store[path]
            return {"deleted": path}
        return None

    def exists(self, path: str) -> bool:
        return path in self._store

    def list(self, prefix: str = "", recursive: bool = True, **kwargs: Any) -> list[FileMetadata]:
        return [meta for path, meta in self._store.items() if path.startswith(prefix)]

    def delete_batch(self, paths: Sequence[str]) -> None:
        for path in paths:
            self._store.pop(path, None)

    def is_implicit_directory(self, path: str) -> bool:
        """Check if path is an implicit directory (has children but no explicit entry)."""
        prefix = path.rstrip("/") + "/"
        return any(p.startswith(prefix) for p in self._store)

    def close(self) -> None:
        self._store.clear()


@pytest.fixture
def nexus_fs(tmp_path):
    """Create a NexusFS instance for consistency testing."""
    backend = LocalBackend(str(tmp_path / "data"))
    metadata_store = InMemoryFileMetadataStore()
    nx = NexusFS(backend=backend, metadata_store=metadata_store, enforce_permissions=False)
    yield nx
    nx.close()


def _ctx(
    consistency: FSConsistency = FSConsistency.CLOSE_TO_OPEN,
    min_zookie: str | None = None,
) -> OperationContext:
    """Create an OperationContext with consistency settings."""
    return OperationContext(
        user="test_user",
        groups=[],
        consistency=consistency,
        min_zookie=min_zookie,
    )


class TestWriteReturnsZookie:
    """Verify write operations return valid zookie tokens."""

    def test_write_result_contains_zookie(self, nexus_fs: NexusFS):
        """Write should return a dict with 'zookie' and 'revision' keys."""
        result = nexus_fs.write("/test.txt", b"hello")
        assert "zookie" in result
        assert "revision" in result
        assert isinstance(result["zookie"], str)
        assert result["zookie"].startswith("nz1.")

    def test_write_zookie_decodes_correctly(self, nexus_fs: NexusFS):
        """Zookie from write should decode to matching zone and revision."""
        result = nexus_fs.write("/test.txt", b"hello")
        zookie = Zookie.decode(result["zookie"])
        assert zookie.revision == result["revision"]
        assert zookie.revision > 0

    def test_write_revisions_increment(self, nexus_fs: NexusFS):
        """Successive writes should produce increasing revisions."""
        r1 = nexus_fs.write("/a.txt", b"a")
        r2 = nexus_fs.write("/b.txt", b"b")
        r3 = nexus_fs.write("/c.txt", b"c")
        assert r1["revision"] < r2["revision"] < r3["revision"]


class TestCTOReadAfterWrite:
    """Core CTO guarantee: write → read with zookie sees fresh data."""

    def test_write_then_read_with_zookie_sees_fresh_data(self, nexus_fs: NexusFS):
        """Read with zookie from the write should see the written content."""
        result = nexus_fs.write("/cto.txt", b"fresh content")
        zookie_token = result["zookie"]

        ctx = _ctx(
            consistency=FSConsistency.CLOSE_TO_OPEN,
            min_zookie=zookie_token,
        )
        content = nexus_fs.read("/cto.txt", context=ctx)
        assert content == b"fresh content"

    def test_read_with_stale_zookie_works(self, nexus_fs: NexusFS):
        """Read with a zookie from an older write should succeed (revision already passed)."""
        r1 = nexus_fs.write("/stale.txt", b"v1")
        old_zookie = r1["zookie"]

        # Do more writes to advance revision
        nexus_fs.write("/other.txt", b"v2")
        nexus_fs.write("/other.txt", b"v3")

        # Read with old zookie — revision is already satisfied
        ctx = _ctx(min_zookie=old_zookie)
        content = nexus_fs.read("/stale.txt", context=ctx)
        assert content == b"v1"

    def test_read_with_strong_consistency(self, nexus_fs: NexusFS):
        """STRONG read with valid zookie should succeed."""
        result = nexus_fs.write("/strong.txt", b"strong data")
        ctx = _ctx(
            consistency=FSConsistency.STRONG,
            min_zookie=result["zookie"],
        )
        content = nexus_fs.read("/strong.txt", context=ctx)
        assert content == b"strong data"

    def test_read_with_eventual_consistency_ignores_zookie(self, nexus_fs: NexusFS):
        """EVENTUAL read should not block on zookie, even if it's from the future."""
        nexus_fs.write("/eventual.txt", b"data")

        # Create a zookie with a very high revision (future)
        future_zookie = Zookie.encode("default", 999999)
        ctx = _ctx(
            consistency=FSConsistency.EVENTUAL,
            min_zookie=future_zookie,
        )
        # Should NOT block or raise — EVENTUAL skips the check entirely
        content = nexus_fs.read("/eventual.txt", context=ctx)
        assert content == b"data"


class TestFutureZookieBehavior:
    """Tests for zookies with revisions ahead of the current state."""

    def test_future_zookie_cto_falls_through(self, nexus_fs: NexusFS):
        """CTO with future zookie should fall through (best-effort, no error)."""
        nexus_fs.write("/future_cto.txt", b"content")

        # Create a zookie far in the future
        future_zookie = Zookie.encode("default", 999999)
        ctx = _ctx(
            consistency=FSConsistency.CLOSE_TO_OPEN,
            min_zookie=future_zookie,
        )
        # CTO: timeout is acceptable, should fall through and return data
        content = nexus_fs.read("/future_cto.txt", context=ctx)
        assert content == b"content"

    def test_future_zookie_strong_raises(self, nexus_fs: NexusFS):
        """STRONG with future zookie should raise ConsistencyTimeoutError."""
        nexus_fs.write("/future_strong.txt", b"content")

        future_zookie = Zookie.encode("default", 999999)
        ctx = _ctx(
            consistency=FSConsistency.STRONG,
            min_zookie=future_zookie,
        )
        with pytest.raises(ConsistencyTimeoutError) as exc_info:
            nexus_fs.read("/future_strong.txt", context=ctx)

        assert exc_info.value.requested_revision == 999999
        assert exc_info.value.zone_id == "default"


class TestInvalidZookieHandling:
    """Tests for invalid/malformed zookie tokens."""

    def test_invalid_zookie_cto_ignores(self, nexus_fs: NexusFS):
        """CTO with invalid zookie should ignore it gracefully (no error)."""
        nexus_fs.write("/invalid_cto.txt", b"data")

        ctx = _ctx(
            consistency=FSConsistency.CLOSE_TO_OPEN,
            min_zookie="not_a_valid_zookie_token",
        )
        # CTO: invalid zookie should be ignored
        content = nexus_fs.read("/invalid_cto.txt", context=ctx)
        assert content == b"data"

    def test_invalid_zookie_strong_raises(self, nexus_fs: NexusFS):
        """STRONG with invalid zookie should raise InvalidZookieError."""
        nexus_fs.write("/invalid_strong.txt", b"data")

        ctx = _ctx(
            consistency=FSConsistency.STRONG,
            min_zookie="not_a_valid_zookie_token",
        )
        with pytest.raises(InvalidZookieError):
            nexus_fs.read("/invalid_strong.txt", context=ctx)


class TestZoneMismatch:
    """Tests for zookies from a different zone."""

    def test_zone_mismatch_zookie_is_ignored(self, nexus_fs: NexusFS):
        """Zookie from a different zone should be ignored (no wait, no error)."""
        nexus_fs.write("/zone_test.txt", b"data")

        # Create a zookie for a different zone with a high revision
        other_zone_zookie = Zookie.encode("other_zone", 999999)
        ctx = _ctx(
            consistency=FSConsistency.STRONG,
            min_zookie=other_zone_zookie,
        )
        # Should NOT block even on STRONG, because the zone doesn't match
        content = nexus_fs.read("/zone_test.txt", context=ctx)
        assert content == b"data"


class TestListWithConsistency:
    """Tests for list operations with CTO consistency."""

    def test_list_with_cto_sees_new_file(self, nexus_fs: NexusFS):
        """Write + list with zookie should see the newly created file."""
        nexus_fs.mkdir("/listdir")
        result = nexus_fs.write("/listdir/new.txt", b"new file")
        zookie_token = result["zookie"]

        ctx = _ctx(min_zookie=zookie_token)
        items = nexus_fs.list("/listdir", context=ctx)
        assert "/listdir/new.txt" in items

    def test_delete_then_list_with_cto_file_gone(self, nexus_fs: NexusFS):
        """Delete + list with zookie should NOT see the deleted file."""
        nexus_fs.mkdir("/deldir")
        nexus_fs.write("/deldir/gone.txt", b"goodbye")

        result = nexus_fs.delete("/deldir/gone.txt")
        zookie_token = result["zookie"]

        ctx = _ctx(min_zookie=zookie_token)
        items = nexus_fs.list("/deldir", context=ctx)
        assert "/deldir/gone.txt" not in items

    def test_list_with_eventual_ignores_future_zookie(self, nexus_fs: NexusFS):
        """EVENTUAL list should not block on future zookie."""
        nexus_fs.mkdir("/evlist")
        nexus_fs.write("/evlist/file.txt", b"data")

        future_zookie = Zookie.encode("default", 999999)
        ctx = _ctx(
            consistency=FSConsistency.EVENTUAL,
            min_zookie=future_zookie,
        )
        # Should return quickly without error
        items = nexus_fs.list("/evlist", context=ctx)
        assert "/evlist/file.txt" in items


class TestAtomicRevisionIncrement:
    """Tests for thread-safe revision incrementing."""

    def test_concurrent_writes_produce_unique_revisions(self, nexus_fs: NexusFS):
        """Concurrent writes should each get a unique, monotonically increasing revision."""
        revisions: list[int] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def write_file(index: int) -> None:
            try:
                result = nexus_fs.write(f"/concurrent_{index}.txt", f"data {index}".encode())
                with lock:
                    revisions.append(result["revision"])
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=write_file, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Unexpected errors: {errors}"
        assert len(revisions) == 10
        # All revisions should be unique
        assert len(set(revisions)) == 10, f"Duplicate revisions: {revisions}"
        # Revisions should be positive
        assert all(r > 0 for r in revisions)
