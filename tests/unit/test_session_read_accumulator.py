"""Unit tests for SessionReadAccumulator (Issue #3417).

Tests TTL expiry, max entries, generation boundaries, concurrent access,
write-clears behavior, and empty accumulator cases.
"""

import threading
import time

from nexus.storage.session_read_accumulator import SessionReadAccumulator


class TestBasicOperations:
    """Basic record/consume operations."""

    def test_record_and_consume(self) -> None:
        acc = SessionReadAccumulator()
        acc.record_read("agent-1", 1, "/a.txt", version=5, content_id="abc")
        acc.record_read("agent-1", 1, "/b.txt", version=3, content_id="def")

        reads = acc.consume("agent-1", 1)
        assert len(reads) == 2
        assert reads[0]["path"] == "/a.txt"
        assert reads[0]["version"] == 5
        assert reads[0]["content_id"] == "abc"
        assert reads[1]["path"] == "/b.txt"

    def test_consume_clears_session(self) -> None:
        acc = SessionReadAccumulator()
        acc.record_read("agent-1", 1, "/a.txt")
        reads = acc.consume("agent-1", 1)
        assert len(reads) == 1

        # Second consume should be empty
        reads = acc.consume("agent-1", 1)
        assert reads == []

    def test_consume_nonexistent_session(self) -> None:
        acc = SessionReadAccumulator()
        reads = acc.consume("no-such-agent", 99)
        assert reads == []

    def test_peek_returns_count(self) -> None:
        acc = SessionReadAccumulator()
        assert acc.peek("agent-1", 1) == 0
        acc.record_read("agent-1", 1, "/a.txt")
        assert acc.peek("agent-1", 1) == 1
        acc.record_read("agent-1", 1, "/b.txt")
        assert acc.peek("agent-1", 1) == 2

    def test_clear_session(self) -> None:
        acc = SessionReadAccumulator()
        acc.record_read("agent-1", 1, "/a.txt")
        acc.clear_session("agent-1", 1)
        assert acc.peek("agent-1", 1) == 0

    def test_record_read_returns_true(self) -> None:
        acc = SessionReadAccumulator()
        result = acc.record_read("agent-1", 1, "/a.txt")
        assert result is True


class TestGenerationBoundary:
    """Session isolation by agent_generation."""

    def test_different_generations_are_isolated(self) -> None:
        acc = SessionReadAccumulator()
        acc.record_read("agent-1", 1, "/gen1.txt")
        acc.record_read("agent-1", 2, "/gen2.txt")

        reads_gen1 = acc.consume("agent-1", 1)
        reads_gen2 = acc.consume("agent-1", 2)

        assert len(reads_gen1) == 1
        assert reads_gen1[0]["path"] == "/gen1.txt"
        assert len(reads_gen2) == 1
        assert reads_gen2[0]["path"] == "/gen2.txt"

    def test_none_generation(self) -> None:
        acc = SessionReadAccumulator()
        acc.record_read("agent-1", None, "/none.txt")
        reads = acc.consume("agent-1", None)
        assert len(reads) == 1
        assert reads[0]["path"] == "/none.txt"


class TestMaxEntries:
    """Max entries per session enforcement."""

    def test_rejects_beyond_max(self) -> None:
        acc = SessionReadAccumulator(max_entries=5)
        for i in range(5):
            assert acc.record_read("agent-1", 1, f"/file_{i}.txt") is True

        # 6th entry should be rejected
        assert acc.record_read("agent-1", 1, "/file_5.txt") is False

    def test_max_entries_does_not_affect_other_sessions(self) -> None:
        acc = SessionReadAccumulator(max_entries=3)
        for i in range(3):
            acc.record_read("agent-1", 1, f"/file_{i}.txt")

        # agent-2 should still be able to record
        assert acc.record_read("agent-2", 1, "/other.txt") is True

    def test_consume_resets_capacity(self) -> None:
        acc = SessionReadAccumulator(max_entries=3)
        for i in range(3):
            acc.record_read("agent-1", 1, f"/file_{i}.txt")
        assert acc.record_read("agent-1", 1, "/overflow.txt") is False

        # Consume and re-record
        acc.consume("agent-1", 1)
        assert acc.record_read("agent-1", 1, "/after_consume.txt") is True


class TestTTL:
    """TTL-based expiry of sessions."""

    def test_expired_sessions_cleaned_up(self) -> None:
        acc = SessionReadAccumulator(ttl_seconds=0.1, sweep_interval=0.0)
        acc.record_read("agent-1", 1, "/a.txt")
        assert acc.peek("agent-1", 1) == 1

        # Wait for TTL to expire
        time.sleep(0.15)

        removed = acc.cleanup_expired()
        assert removed == 1
        assert acc.peek("agent-1", 1) == 0

    def test_active_sessions_not_cleaned(self) -> None:
        acc = SessionReadAccumulator(ttl_seconds=10.0)
        acc.record_read("agent-1", 1, "/a.txt")

        removed = acc.cleanup_expired()
        assert removed == 0
        assert acc.peek("agent-1", 1) == 1

    def test_lazy_sweep_on_access(self) -> None:
        acc = SessionReadAccumulator(ttl_seconds=0.05, sweep_interval=0.0)
        acc.record_read("agent-old", 1, "/old.txt")
        time.sleep(0.1)

        # Recording for a new agent triggers lazy sweep
        acc.record_read("agent-new", 1, "/new.txt")

        # Old session should have been cleaned up
        assert acc.peek("agent-old", 1) == 0
        assert acc.peek("agent-new", 1) == 1


class TestConcurrentAccess:
    """Thread-safety verification."""

    def test_concurrent_records(self) -> None:
        acc = SessionReadAccumulator(max_entries=10000)
        errors: list[Exception] = []

        def record_batch(agent_id: str, count: int) -> None:
            try:
                for i in range(count):
                    acc.record_read(agent_id, 1, f"/file_{agent_id}_{i}.txt")
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=record_batch, args=(f"agent-{t}", 100)) for t in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0

        # Verify each agent has exactly 100 reads
        for t in range(10):
            reads = acc.consume(f"agent-{t}", 1)
            assert len(reads) == 100

    def test_concurrent_record_and_consume(self) -> None:
        acc = SessionReadAccumulator()
        consumed_counts: list[int] = []
        errors: list[Exception] = []

        def producer() -> None:
            try:
                for i in range(50):
                    acc.record_read("agent-1", 1, f"/file_{i}.txt")
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        def consumer() -> None:
            try:
                time.sleep(0.025)  # Let some records accumulate
                reads = acc.consume("agent-1", 1)
                consumed_counts.append(len(reads))
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=producer)
        t2 = threading.Thread(target=consumer)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(errors) == 0
        # Consumer should have gotten some reads
        assert len(consumed_counts) == 1
        assert consumed_counts[0] > 0


class TestStats:
    """Statistics reporting."""

    def test_stats_structure(self) -> None:
        acc = SessionReadAccumulator()
        stats = acc.get_stats()
        assert "active_sessions" in stats
        assert "total_entries" in stats
        assert stats["active_sessions"] == 0
        assert stats["total_entries"] == 0

    def test_stats_after_records(self) -> None:
        acc = SessionReadAccumulator()
        acc.record_read("agent-1", 1, "/a.txt")
        acc.record_read("agent-2", 1, "/b.txt")

        stats = acc.get_stats()
        assert stats["active_sessions"] == 2
        assert stats["total_entries"] == 2


class TestEmptyAccumulator:
    """Edge case: agent writes without reading anything."""

    def test_consume_empty_returns_empty_list(self) -> None:
        acc = SessionReadAccumulator()
        # Create session by recording nothing (agent connects but doesn't read)
        reads = acc.consume("agent-empty", 1)
        assert reads == []

    def test_no_error_on_repeated_consume(self) -> None:
        acc = SessionReadAccumulator()
        for _ in range(5):
            reads = acc.consume("agent-empty", 1)
            assert reads == []


class TestScopedTracking:
    """Scoped lineage tracking — per-task read isolation."""

    def test_begin_scope_sets_active(self) -> None:
        acc = SessionReadAccumulator()
        assert acc.get_active_scope("agent-1", 1) == "_default"
        acc.begin_scope("agent-1", 1, "task-A")
        assert acc.get_active_scope("agent-1", 1) == "task-A"

    def test_reads_go_into_active_scope(self) -> None:
        acc = SessionReadAccumulator()
        acc.begin_scope("agent-1", 1, "task-A")
        acc.record_read("agent-1", 1, "/a.txt")
        acc.record_read("agent-1", 1, "/b.txt")

        assert acc.peek("agent-1", 1, scope_id="task-A") == 2
        assert acc.peek("agent-1", 1, scope_id="_default") == 0

    def test_scope_isolation(self) -> None:
        """Reads in different scopes are isolated."""
        acc = SessionReadAccumulator()

        acc.begin_scope("agent-1", 1, "task-A")
        acc.record_read("agent-1", 1, "/a1.txt")
        acc.record_read("agent-1", 1, "/a2.txt")

        acc.begin_scope("agent-1", 1, "task-B")
        acc.record_read("agent-1", 1, "/b1.txt")

        reads_a = acc.consume("agent-1", 1, scope_id="task-A")
        reads_b = acc.consume("agent-1", 1, scope_id="task-B")

        assert len(reads_a) == 2
        assert reads_a[0]["path"] == "/a1.txt"
        assert reads_a[1]["path"] == "/a2.txt"
        assert len(reads_b) == 1
        assert reads_b[0]["path"] == "/b1.txt"

    def test_consume_only_clears_target_scope(self) -> None:
        """Consuming one scope doesn't affect other scopes."""
        acc = SessionReadAccumulator()

        acc.begin_scope("agent-1", 1, "task-A")
        acc.record_read("agent-1", 1, "/a.txt")
        acc.begin_scope("agent-1", 1, "task-B")
        acc.record_read("agent-1", 1, "/b.txt")

        # Consume task-A only
        acc.consume("agent-1", 1, scope_id="task-A")

        # task-B should still have its read
        assert acc.peek("agent-1", 1, scope_id="task-B") == 1
        reads_b = acc.consume("agent-1", 1, scope_id="task-B")
        assert len(reads_b) == 1

    def test_read3_write1_write2_scenario(self) -> None:
        """The key scenario: read 3 → write 1 → write 2.

        With scopes, each write gets its own reads.
        Without scopes, write 2 gets nothing (old bug).
        """
        acc = SessionReadAccumulator()

        # Task 1: read A, B, C → write output1
        acc.begin_scope("agent-1", 1, "task-1")
        acc.record_read("agent-1", 1, "/a.csv")
        acc.record_read("agent-1", 1, "/b.csv")
        acc.record_read("agent-1", 1, "/c.csv")

        reads_1 = acc.consume("agent-1", 1, scope_id="task-1")
        assert len(reads_1) == 3

        # Task 2: read D, E → write output2
        acc.begin_scope("agent-1", 1, "task-2")
        acc.record_read("agent-1", 1, "/d.csv")
        acc.record_read("agent-1", 1, "/e.csv")

        reads_2 = acc.consume("agent-1", 1, scope_id="task-2")
        assert len(reads_2) == 2
        assert reads_2[0]["path"] == "/d.csv"
        assert reads_2[1]["path"] == "/e.csv"

    def test_default_scope_backward_compat(self) -> None:
        """Without begin_scope, reads go into _default — same as old behavior."""
        acc = SessionReadAccumulator()
        acc.record_read("agent-1", 1, "/a.txt")
        acc.record_read("agent-1", 1, "/b.txt")

        # consume() without scope_id uses active scope (which is _default)
        reads = acc.consume("agent-1", 1)
        assert len(reads) == 2

    def test_end_scope_consumes_and_removes(self) -> None:
        acc = SessionReadAccumulator()
        acc.begin_scope("agent-1", 1, "task-A")
        acc.record_read("agent-1", 1, "/a.txt")

        reads = acc.end_scope("agent-1", 1, "task-A")
        assert len(reads) == 1

        # Scope is gone — peek returns 0
        assert acc.peek("agent-1", 1, scope_id="task-A") == 0
        # Active scope reverted to default
        assert acc.get_active_scope("agent-1", 1) == "_default"

    def test_end_scope_nonexistent_returns_empty(self) -> None:
        acc = SessionReadAccumulator()
        reads = acc.end_scope("agent-1", 1, "no-such-scope")
        assert reads == []

    def test_explicit_scope_id_on_record(self) -> None:
        """record_read with explicit scope_id overrides active scope."""
        acc = SessionReadAccumulator()
        acc.begin_scope("agent-1", 1, "task-A")

        # Record into task-B explicitly (even though task-A is active)
        acc.record_read("agent-1", 1, "/b.txt", scope_id="task-B")

        assert acc.peek("agent-1", 1, scope_id="task-A") == 0
        assert acc.peek("agent-1", 1, scope_id="task-B") == 1

    def test_reactivate_existing_scope(self) -> None:
        """begin_scope on existing scope reactivates it (appends, doesn't clear)."""
        acc = SessionReadAccumulator()
        acc.begin_scope("agent-1", 1, "task-A")
        acc.record_read("agent-1", 1, "/first.txt")

        acc.begin_scope("agent-1", 1, "task-B")
        acc.record_read("agent-1", 1, "/other.txt")

        # Reactivate task-A
        acc.begin_scope("agent-1", 1, "task-A")
        acc.record_read("agent-1", 1, "/second.txt")

        reads = acc.consume("agent-1", 1, scope_id="task-A")
        assert len(reads) == 2
        assert reads[0]["path"] == "/first.txt"
        assert reads[1]["path"] == "/second.txt"

    def test_max_entries_shared_across_scopes(self) -> None:
        """Max entries limit applies across all scopes in a session."""
        acc = SessionReadAccumulator(max_entries=5)
        acc.begin_scope("agent-1", 1, "task-A")
        for i in range(3):
            acc.record_read("agent-1", 1, f"/a{i}.txt")

        acc.begin_scope("agent-1", 1, "task-B")
        for i in range(2):
            acc.record_read("agent-1", 1, f"/b{i}.txt")

        # 6th entry should fail (5 total across both scopes)
        assert acc.record_read("agent-1", 1, "/overflow.txt") is False
