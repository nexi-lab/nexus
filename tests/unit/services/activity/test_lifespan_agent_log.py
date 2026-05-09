import pytest

from nexus.services.activity import lifespan
from nexus.services.activity.sinks.jsonl import JsonlActivitySink


@pytest.mark.asyncio
async def test_setup_registers_jsonl_sink(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", str(tmp_path / "a.db"))
    monkeypatch.setenv("NEXUS_ACTIVITY_AGENT_LOG_ENABLED", "1")
    await lifespan.setup_activity()
    try:
        worker = lifespan._STATE["worker"]
        # ActivityWorker exposes its sinks via _sinks (private). If a public
        # accessor exists, prefer it; otherwise this is acceptable for an
        # internal smoke test.
        sinks = getattr(worker, "_sinks", None) or getattr(worker, "sinks", None)
        assert sinks is not None, "worker must expose sinks list"
        assert any(isinstance(s, JsonlActivitySink) for s in sinks)
    finally:
        await lifespan.shutdown_activity()


@pytest.mark.asyncio
async def test_setup_skips_jsonl_sink_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", str(tmp_path / "a.db"))
    monkeypatch.setenv("NEXUS_ACTIVITY_AGENT_LOG_ENABLED", "0")
    await lifespan.setup_activity()
    try:
        worker = lifespan._STATE["worker"]
        sinks = getattr(worker, "_sinks", None) or getattr(worker, "sinks", None)
        assert sinks is not None
        assert not any(isinstance(s, JsonlActivitySink) for s in sinks)
        assert lifespan.get_agent_log_store() is None
    finally:
        await lifespan.shutdown_activity()


@pytest.mark.asyncio
async def test_setup_exposes_store_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", str(tmp_path / "a.db"))
    monkeypatch.setenv("NEXUS_ACTIVITY_AGENT_LOG_ENABLED", "1")
    await lifespan.setup_activity()
    try:
        store = lifespan.get_agent_log_store()
        assert store is not None
    finally:
        await lifespan.shutdown_activity()


@pytest.mark.asyncio
async def test_shutdown_clears_store(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", str(tmp_path / "a.db"))
    monkeypatch.setenv("NEXUS_ACTIVITY_AGENT_LOG_ENABLED", "1")
    await lifespan.setup_activity()
    await lifespan.shutdown_activity()
    assert lifespan.get_agent_log_store() is None
