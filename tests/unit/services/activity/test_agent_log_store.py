from nexus.services.activity.agent_log_store import MemoryBackend


def test_append_and_read_one_line():
    store = MemoryBackend(cap_bytes=1024)
    store.append_line("alice", "2026-05-09", b'{"ts":"x"}\n')
    out = store.read_path("/.activity/2026-05-09/alice.jsonl")
    assert out == b'{"ts":"x"}\n'


def test_per_agent_isolation():
    store = MemoryBackend(cap_bytes=1024)
    store.append_line("alice", "2026-05-09", b"a\n")
    store.append_line("bob", "2026-05-09", b"b\n")
    assert store.read_path("/.activity/2026-05-09/alice.jsonl") == b"a\n"
    assert store.read_path("/.activity/2026-05-09/bob.jsonl") == b"b\n"


def test_per_date_isolation():
    store = MemoryBackend(cap_bytes=1024)
    store.append_line("alice", "2026-05-09", b"d9\n")
    store.append_line("alice", "2026-05-10", b"d10\n")
    assert store.read_path("/.activity/2026-05-09/alice.jsonl") == b"d9\n"
    assert store.read_path("/.activity/2026-05-10/alice.jsonl") == b"d10\n"


def test_ring_buffer_evicts_oldest():
    store = MemoryBackend(cap_bytes=10)
    for i in range(5):
        store.append_line("alice", "2026-05-09", f"line{i}\n".encode())
    out = store.read_path("/.activity/2026-05-09/alice.jsonl")
    # Last lines preserved, earliest evicted, total <= cap
    assert b"line4\n" in out
    assert len(out) <= 10
    # Always at least one line
    assert out


def test_ring_buffer_keeps_at_least_one_line_when_single_line_exceeds_cap():
    store = MemoryBackend(cap_bytes=4)
    store.append_line("alice", "2026-05-09", b"this_line_is_long\n")
    out = store.read_path("/.activity/2026-05-09/alice.jsonl")
    assert out == b"this_line_is_long\n"


def test_read_unknown_path_returns_empty():
    store = MemoryBackend(cap_bytes=1024)
    assert store.read_path("/.activity/2026-05-09/ghost.jsonl") == b""


def test_list_dir_root_returns_dates():
    store = MemoryBackend(cap_bytes=1024)
    store.append_line("alice", "2026-05-09", b"a\n")
    store.append_line("alice", "2026-05-10", b"a\n")
    assert sorted(store.list_dir("/.activity/")) == ["2026-05-09", "2026-05-10"]


def test_list_dir_date_returns_agent_files():
    store = MemoryBackend(cap_bytes=1024)
    store.append_line("alice", "2026-05-09", b"a\n")
    store.append_line("bob", "2026-05-09", b"b\n")
    assert sorted(store.list_dir("/.activity/2026-05-09/")) == ["alice.jsonl", "bob.jsonl"]


def test_evicted_count_increments():
    store = MemoryBackend(cap_bytes=8)
    store.append_line("alice", "2026-05-09", b"line1\n")  # 6 bytes
    store.append_line("alice", "2026-05-09", b"line2\n")  # +6 = 12, evict line1
    assert store.lines_evicted == 1


def test_drop_date_removes_buffer():
    store = MemoryBackend(cap_bytes=1024)
    store.append_line("alice", "2026-05-09", b"a\n")
    store.drop_date("2026-05-09")
    assert store.read_path("/.activity/2026-05-09/alice.jsonl") == b""


def test_path_with_extra_slashes_returns_empty():
    store = MemoryBackend(cap_bytes=1024)
    store.append_line("alice", "2026-05-09", b"a\n")
    assert store.read_path("/.activity/2026-05-09/extra/alice.jsonl") == b""


def test_drop_date_releases_lock_entry():
    store = MemoryBackend(cap_bytes=1024)
    store.append_line("alice", "2026-05-09", b"a\n")
    store.drop_date("2026-05-09")
    # Internal lock dict should not retain stale keys for dropped dates.
    assert not any(k.date == "2026-05-09" for k in store._locks)
