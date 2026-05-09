from datetime import UTC, datetime, timedelta

from nexus.services.activity.agent_log_store import MemoryBackend
from nexus.services.activity.retention import sweep_agent_log


def test_sweep_drops_dates_older_than_retention():
    store = MemoryBackend(cap_bytes=1024)
    today = datetime.now(UTC).date()
    old = (today - timedelta(days=10)).isoformat()
    young = (today - timedelta(days=1)).isoformat()

    store.append_line("alice", old, b"old\n")
    store.append_line("alice", young, b"young\n")

    sweep_agent_log(store, retention_days=7, now=datetime.now(UTC))

    assert store.read_path(f"/.activity/{old}/alice.jsonl") == b""
    assert store.read_path(f"/.activity/{young}/alice.jsonl") == b"young\n"


def test_sweep_keeps_today():
    store = MemoryBackend(cap_bytes=1024)
    today = datetime.now(UTC).date().isoformat()
    store.append_line("alice", today, b"today\n")
    sweep_agent_log(store, retention_days=7, now=datetime.now(UTC))
    assert store.read_path(f"/.activity/{today}/alice.jsonl") == b"today\n"


def test_sweep_returns_count_dropped():
    store = MemoryBackend(cap_bytes=1024)
    today = datetime.now(UTC).date()
    old1 = (today - timedelta(days=20)).isoformat()
    old2 = (today - timedelta(days=15)).isoformat()
    young = (today - timedelta(days=1)).isoformat()

    store.append_line("alice", old1, b"x\n")
    store.append_line("alice", old2, b"x\n")
    store.append_line("alice", young, b"x\n")

    dropped = sweep_agent_log(store, retention_days=7, now=datetime.now(UTC))
    assert dropped == 2


def test_sweep_idempotent():
    store = MemoryBackend(cap_bytes=1024)
    today = datetime.now(UTC).date()
    old = (today - timedelta(days=10)).isoformat()
    store.append_line("alice", old, b"x\n")

    first = sweep_agent_log(store, retention_days=7, now=datetime.now(UTC))
    second = sweep_agent_log(store, retention_days=7, now=datetime.now(UTC))
    assert first == 1
    assert second == 0
