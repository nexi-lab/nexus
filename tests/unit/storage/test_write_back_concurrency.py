"""Tests for concurrent write-back behavior in BufferedMetadataStore.

Validates thread-safety of the deferred write-back buffer under
concurrent writes, reads, flushes, and batch-overflow scenarios.
"""

import threading
import time
from datetime import UTC, datetime

from nexus.contracts.metadata import FileMetadata
from nexus.storage.buffered_metadata_store import BufferedMetadataStore
from tests.helpers.dict_metastore import DictMetastore


def _make_metadata(path: str, version: int = 1, content_hash: str = "hash") -> FileMetadata:
    return FileMetadata(
        path=path,
        backend_name="test-backend",
        physical_path=f"/phys{path}",
        size=100,
        etag=content_hash,
        created_at=datetime.now(UTC),
        modified_at=datetime.now(UTC),
        version=version,
        zone_id="zone-1",
        owner_id="owner-1",
    )


class TestWriteBackConcurrency:
    """Concurrent write-back tests for BufferedMetadataStore."""

    def test_concurrent_writes_to_different_paths(self) -> None:
        """10 threads each writing to different paths with consistency='wb'.

        All writes should be visible via get().
        """
        inner = DictMetastore()
        store = BufferedMetadataStore(inner, flush_interval_sec=10.0, max_batch_size=200)

        num_threads = 10
        barrier = threading.Barrier(num_threads)
        errors: list[Exception] = []

        def writer(thread_id: int) -> None:
            try:
                barrier.wait(timeout=5.0)
                path = f"/file_{thread_id}.txt"
                meta = _make_metadata(path, version=thread_id + 1, content_hash=f"hash_{thread_id}")
                store.put(meta, consistency="wb")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert not errors, f"Writer threads raised errors: {errors}"

        # All writes should be visible via get() (read overlay on pending buffer)
        for i in range(num_threads):
            path = f"/file_{i}.txt"
            result = store.get(path)
            assert result is not None, f"get({path}) returned None"
            assert result.path == path
            assert result.version == i + 1
            assert result.etag == f"hash_{i}"

    def test_concurrent_writes_to_same_path_last_writer_wins(self) -> None:
        """Multiple threads writing to the same path.

        After all complete, get() should return one of the written values
        (last-writer-wins semantics).
        """
        inner = DictMetastore()
        store = BufferedMetadataStore(inner, flush_interval_sec=10.0, max_batch_size=200)

        path = "/shared_file.txt"
        num_threads = 20
        barrier = threading.Barrier(num_threads)
        errors: list[Exception] = []

        def writer(thread_id: int) -> None:
            try:
                barrier.wait(timeout=5.0)
                meta = _make_metadata(path, version=thread_id + 1, content_hash=f"hash_{thread_id}")
                store.put(meta, consistency="wb")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert not errors, f"Writer threads raised errors: {errors}"

        result = store.get(path)
        assert result is not None, "get() returned None for shared path"
        assert result.path == path
        # The version should be one of the values written by any thread
        valid_versions = set(range(1, num_threads + 1))
        assert result.version in valid_versions, (
            f"Unexpected version {result.version}; expected one of {valid_versions}"
        )

    def test_concurrent_writes_and_reads(self) -> None:
        """Writers and readers running concurrently.

        Readers should never get None for a path that was written to
        (once a wb write for a path has returned, subsequent reads must see it).
        """
        inner = DictMetastore()
        store = BufferedMetadataStore(inner, flush_interval_sec=10.0, max_batch_size=200)

        num_writers = 5
        num_readers = 5
        reads_per_reader = 50
        # Events to signal that each writer has finished
        writer_done_events = [threading.Event() for _ in range(num_writers)]
        errors: list[str] = []

        def writer(thread_id: int) -> None:
            path = f"/rw_file_{thread_id}.txt"
            meta = _make_metadata(path, version=1, content_hash=f"hash_{thread_id}")
            store.put(meta, consistency="wb")
            writer_done_events[thread_id].set()

        def reader(thread_id: int) -> None:
            # Pick the writer we pair with (round-robin)
            writer_id = thread_id % num_writers
            path = f"/rw_file_{writer_id}.txt"
            # Wait until the paired writer has completed its put()
            writer_done_events[writer_id].wait(timeout=5.0)
            for _ in range(reads_per_reader):
                result = store.get(path)
                if result is None:
                    errors.append(f"Reader {thread_id} got None for {path} after writer completed")
                    return

        writer_threads = [threading.Thread(target=writer, args=(i,)) for i in range(num_writers)]
        reader_threads = [threading.Thread(target=reader, args=(i,)) for i in range(num_readers)]

        # Start all threads
        for t in writer_threads + reader_threads:
            t.start()
        for t in writer_threads + reader_threads:
            t.join(timeout=10.0)

        assert not errors, f"Read-after-write violations: {errors}"

    def test_concurrent_writes_sequential_versions(self) -> None:
        """Write multiple versions to the same path sequentially.

        Verify version numbers are preserved correctly after flush.
        """
        inner = DictMetastore()
        store = BufferedMetadataStore(inner, flush_interval_sec=10.0, max_batch_size=200)

        path = "/versioned_file.txt"
        num_versions = 20

        for v in range(1, num_versions + 1):
            meta = _make_metadata(path, version=v, content_hash=f"hash_v{v}")
            store.put(meta, consistency="wb")

            # Each put should immediately be visible via get() (buffer overlay)
            result = store.get(path)
            assert result is not None, f"get() returned None after writing version {v}"
            assert result.version == v, f"Expected version {v} after write, got {result.version}"

        # Last-writer-wins: only the final version should remain in the buffer
        result = store.get(path)
        assert result is not None
        assert result.version == num_versions
        assert result.etag == f"hash_v{num_versions}"

        # After flush, the inner store should have the final version
        store.flush()
        inner_result = inner.get(path)
        assert inner_result is not None, "Inner store missing entry after flush"
        assert inner_result.version == num_versions
        assert inner_result.etag == f"hash_v{num_versions}"

    def test_flush_during_writes(self) -> None:
        """Start a background flush thread while writers are actively enqueueing.

        Should not crash or lose data.
        """
        inner = DictMetastore()
        store = BufferedMetadataStore(inner, flush_interval_sec=10.0, max_batch_size=200)

        num_writers = 5
        writes_per_writer = 20
        errors: list[Exception] = []
        writer_start = threading.Event()

        def writer(thread_id: int) -> None:
            try:
                writer_start.wait(timeout=5.0)
                for i in range(writes_per_writer):
                    path = f"/flush_test_{thread_id}_{i}.txt"
                    meta = _make_metadata(path, version=1, content_hash=f"h_{thread_id}_{i}")
                    store.put(meta, consistency="wb")
            except Exception as e:
                errors.append(e)

        def flusher() -> None:
            try:
                writer_start.wait(timeout=5.0)
                # Repeatedly flush while writers are active
                for _ in range(10):
                    store.flush()
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        writer_threads = [threading.Thread(target=writer, args=(i,)) for i in range(num_writers)]
        flush_thread = threading.Thread(target=flusher)

        for t in writer_threads:
            t.start()
        flush_thread.start()

        # Unleash all threads simultaneously
        writer_start.set()

        for t in writer_threads:
            t.join(timeout=10.0)
        flush_thread.join(timeout=10.0)

        assert not errors, f"Threads raised errors: {errors}"

        # Final flush to push any remaining buffered items
        store.flush()

        # Every path should exist in the inner store after final flush
        for tid in range(num_writers):
            for i in range(writes_per_writer):
                path = f"/flush_test_{tid}_{i}.txt"
                result = inner.get(path)
                assert result is not None, f"Path {path} lost after concurrent flush + writes"

    def test_batch_overflow_triggers_flush_event(self) -> None:
        """Write more items than max_batch_size.

        The flush event should be triggered, causing items to eventually
        appear in the inner store without an explicit flush() call.
        """
        inner = DictMetastore()
        max_batch = 5
        store = BufferedMetadataStore(inner, flush_interval_sec=10.0, max_batch_size=max_batch)
        # Start the background flush thread so it can react to the flush event
        store._start_sync()

        try:
            # Write more than max_batch_size items to trigger overflow
            num_items = max_batch + 3
            for i in range(num_items):
                path = f"/overflow_{i}.txt"
                meta = _make_metadata(path, version=1, content_hash=f"h_{i}")
                store.put(meta, consistency="wb")

            # Give the background flush thread time to react to the overflow event
            deadline = time.monotonic() + 5.0
            flushed = False
            while time.monotonic() < deadline:
                # Check if at least max_batch_size items made it to the inner store
                found = sum(
                    1 for i in range(num_items) if inner.get(f"/overflow_{i}.txt") is not None
                )
                if found >= max_batch:
                    flushed = True
                    break
                time.sleep(0.01)

            assert flushed, (
                f"Expected at least {max_batch} items flushed to inner store "
                f"via batch overflow, but found fewer"
            )
        finally:
            store._stop_sync()
