"""Foundation tests for InvalidationStream (DT_STREAM).

Establishes behavioral baseline before the durable channel refactor.
Covers: append ordering, consumer offsets, replay, concurrent access,
deque overflow, and failure isolation.

Related: Issue #3396 (decision 9A)
"""

import threading

from nexus.bricks.rebac.cache.invalidation_stream import (
    InvalidationEventType,
    InvalidationStream,
)


class TestInvalidationStreamBasic:
    """Basic stream operations."""

    def test_append_returns_monotonic_sequences(self):
        stream = InvalidationStream(max_size=100)
        s1 = stream.append(InvalidationEventType.L1_CACHE, "zone-a", key="val1")
        s2 = stream.append(InvalidationEventType.BOUNDARY, "zone-a", key="val2")
        s3 = stream.append(InvalidationEventType.VISIBILITY, "zone-b", key="val3")
        assert s1 == 1
        assert s2 == 2
        assert s3 == 3

    def test_append_creates_event_with_correct_fields(self):
        stream = InvalidationStream(max_size=100)
        received = []
        stream.register_consumer("c1", received.append)
        stream.append(InvalidationEventType.NAMESPACE, "zone-x", foo="bar", baz=42)

        assert len(received) == 1
        event = received[0]
        assert event.event_type == InvalidationEventType.NAMESPACE
        assert event.zone_id == "zone-x"
        assert event.payload == {"foo": "bar", "baz": 42}
        assert event.sequence == 1
        assert event.timestamp > 0

    def test_empty_stream_stats(self):
        stream = InvalidationStream(max_size=50)
        stats = stream.get_stats()
        assert stats["total_events"] == 0
        assert stats["stream_size"] == 0
        assert stats["max_size"] == 50
        assert stats["current_sequence"] == 0
        assert stats["consumer_count"] == 0


class TestInvalidationStreamConsumers:
    """Consumer registration, dispatch, and offset tracking."""

    def test_consumer_receives_events_in_order(self):
        stream = InvalidationStream(max_size=100)
        events = []
        stream.register_consumer("c1", events.append)

        stream.append(InvalidationEventType.L1_CACHE, "z", seq=1)
        stream.append(InvalidationEventType.BOUNDARY, "z", seq=2)
        stream.append(InvalidationEventType.VISIBILITY, "z", seq=3)

        assert len(events) == 3
        assert [e.payload["seq"] for e in events] == [1, 2, 3]

    def test_multiple_consumers_all_receive_events(self):
        stream = InvalidationStream(max_size=100)
        c1_events, c2_events = [], []
        stream.register_consumer("c1", c1_events.append)
        stream.register_consumer("c2", c2_events.append)

        stream.append(InvalidationEventType.L1_CACHE, "z", key="v")

        assert len(c1_events) == 1
        assert len(c2_events) == 1

    def test_consumer_offset_tracks_last_processed(self):
        stream = InvalidationStream(max_size=100)
        stream.register_consumer("c1", lambda e: None)

        stream.append(InvalidationEventType.L1_CACHE, "z")
        stream.append(InvalidationEventType.L1_CACHE, "z")

        stats = stream.get_stats()
        assert stats["consumer_offsets"]["c1"] == 2

    def test_unregister_consumer_stops_delivery(self):
        stream = InvalidationStream(max_size=100)
        events = []
        stream.register_consumer("c1", events.append)
        stream.append(InvalidationEventType.L1_CACHE, "z")
        assert len(events) == 1

        stream.unregister_consumer("c1")
        stream.append(InvalidationEventType.L1_CACHE, "z")
        assert len(events) == 1  # No new events

    def test_consumer_failure_isolation(self):
        """One failing consumer doesn't block others."""
        stream = InvalidationStream(max_size=100)
        good_events = []

        def bad_consumer(event):
            raise RuntimeError("boom")

        stream.register_consumer("bad", bad_consumer)
        stream.register_consumer("good", good_events.append)

        stream.append(InvalidationEventType.L1_CACHE, "z", key="v")

        assert len(good_events) == 1
        assert stream.get_stats()["consumer_errors"] == 1

    def test_late_consumer_starts_at_current_sequence(self):
        """Consumer registered after events only gets future events."""
        stream = InvalidationStream(max_size=100)
        stream.append(InvalidationEventType.L1_CACHE, "z")  # seq 1
        stream.append(InvalidationEventType.L1_CACHE, "z")  # seq 2

        events = []
        stream.register_consumer("late", events.append)

        stream.append(InvalidationEventType.L1_CACHE, "z")  # seq 3
        assert len(events) == 1
        assert events[0].sequence == 3


class TestInvalidationStreamReplay:
    """Replay capability for failure recovery."""

    def test_replay_from_sequence(self):
        stream = InvalidationStream(max_size=100)
        events = []
        stream.register_consumer("c1", events.append)

        # Append 5 events
        for i in range(5):
            stream.append(InvalidationEventType.L1_CACHE, "z", idx=i)

        assert len(events) == 5
        events.clear()

        # Replay from sequence 2 (should replay 3, 4, 5)
        replayed = stream.replay_from("c1", 2)
        assert replayed == 3
        assert len(events) == 3
        assert [e.payload["idx"] for e in events] == [2, 3, 4]

    def test_replay_stops_on_consumer_error(self):
        stream = InvalidationStream(max_size=100)
        call_count = 0

        def failing_after_2(event):
            nonlocal call_count
            call_count += 1
            if call_count > 2:
                raise RuntimeError("replay fail")

        stream.register_consumer("c1", failing_after_2)

        for i in range(5):
            stream.append(InvalidationEventType.L1_CACHE, "z", idx=i)

        call_count = 0  # Reset for replay
        replayed = stream.replay_from("c1", 0)
        assert replayed == 2  # Stopped after error on 3rd

    def test_replay_unknown_consumer_returns_zero(self):
        stream = InvalidationStream(max_size=100)
        assert stream.replay_from("unknown", 0) == 0


class TestInvalidationStreamOverflow:
    """Deque maxlen trimming behavior."""

    def test_old_events_trimmed_when_maxlen_exceeded(self):
        stream = InvalidationStream(max_size=3)

        stream.register_consumer("c1", lambda e: None)

        for i in range(5):
            stream.append(InvalidationEventType.L1_CACHE, "z", idx=i)

        stats = stream.get_stats()
        assert stats["stream_size"] == 3  # Only last 3 retained
        assert stats["total_events"] == 5  # 5 total appended
        assert stats["current_sequence"] == 5

    def test_replay_limited_to_retained_events(self):
        stream = InvalidationStream(max_size=3)
        events = []
        stream.register_consumer("c1", events.append)

        for i in range(5):
            stream.append(InvalidationEventType.L1_CACHE, "z", idx=i)

        events.clear()
        replayed = stream.replay_from("c1", 0)
        # Only events with sequence 3, 4, 5 are retained (maxlen=3)
        assert replayed == 3


class TestInvalidationStreamConcurrency:
    """Thread-safety of concurrent append and consume."""

    def test_concurrent_appends(self):
        stream = InvalidationStream(max_size=10_000)
        events = []
        lock = threading.Lock()

        def safe_append(event):
            with lock:
                events.append(event)

        stream.register_consumer("c1", safe_append)

        threads = []
        n_per_thread = 100
        n_threads = 10

        def producer(thread_id):
            for i in range(n_per_thread):
                stream.append(InvalidationEventType.L1_CACHE, "z", tid=thread_id, idx=i)

        for t in range(n_threads):
            threads.append(threading.Thread(target=producer, args=(t,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(events) == n_threads * n_per_thread
        # Sequences should be unique
        sequences = {e.sequence for e in events}
        assert len(sequences) == n_threads * n_per_thread
