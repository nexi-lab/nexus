"""Unit tests for lineage post-flush hook (Issue #3417)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.contracts.aspects import AspectRegistry, LineageAspect
from nexus.storage.lineage_service import LineageService
from nexus.storage.models._base import Base
from nexus.storage.session_read_accumulator import reset_accumulator


@pytest.fixture(autouse=True)
def _reset_accumulator():
    """Reset the global accumulator before each test."""
    reset_accumulator()
    yield
    reset_accumulator()


@pytest.fixture()
def db_engine():
    """Create an in-memory SQLite engine with all tables."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture()
def session_factory(db_engine):
    """Create a session factory for the test database."""
    from contextlib import contextmanager

    SessionLocal = sessionmaker(bind=db_engine)

    @contextmanager
    def factory():
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    return factory


class TestLineageHookIntegration:
    """Test the lineage hook batch recording function."""

    def test_record_lineage_batch_for_agent_write(self, db_engine, session_factory) -> None:
        """Simulate: agent reads files, then writes output. Lineage is recorded."""
        from nexus.factory._lineage_hook import _record_lineage_batch
        from nexus.storage.session_read_accumulator import get_accumulator

        # Ensure lineage aspect is registered
        registry = AspectRegistry.get()
        if not registry.is_registered("lineage"):
            registry.register("lineage", LineageAspect, max_versions=5)

        # Simulate agent reads
        acc = get_accumulator()
        acc.record_read("agent-1", 1, "/data/input.csv", version=5, content_id="abc123")
        acc.record_read("agent-1", 1, "/data/config.yaml", version=3, content_id="def456")

        # Simulate the post-flush event for a write
        events = [
            {
                "op": "write",
                "path": "/output/result.json",
                "zone_id": "root",
                "agent_id": "agent-1",
                "agent_generation": 1,
                "metadata": {},
            }
        ]

        _record_lineage_batch(events, session_factory)

        # Verify lineage was recorded
        with session_factory() as session:
            svc = LineageService(session)
            from nexus.contracts.urn import NexusURN

            urn = str(NexusURN.for_file("root", "/output/result.json"))
            lineage = svc.get_lineage(urn)
            assert lineage is not None
            assert len(lineage["upstream"]) == 2
            assert lineage["agent_id"] == "agent-1"

            # Verify reverse index
            downstream = svc.find_downstream("/data/input.csv")
            assert len(downstream) == 1

    def test_no_lineage_for_non_agent_writes(self, db_engine, session_factory) -> None:
        """Events without agent_id should produce no lineage."""
        from nexus.factory._lineage_hook import _record_lineage_batch

        events = [
            {
                "op": "write",
                "path": "/output/result.json",
                "zone_id": "root",
                "agent_id": None,
                "metadata": {},
            }
        ]

        _record_lineage_batch(events, session_factory)

        with session_factory() as session:
            svc = LineageService(session)
            from nexus.contracts.urn import NexusURN

            urn = str(NexusURN.for_file("root", "/output/result.json"))
            assert svc.get_lineage(urn) is None

    def test_no_lineage_when_no_reads(self, db_engine, session_factory) -> None:
        """Agent writes without reading anything -> no lineage."""
        from nexus.factory._lineage_hook import _record_lineage_batch

        registry = AspectRegistry.get()
        if not registry.is_registered("lineage"):
            registry.register("lineage", LineageAspect, max_versions=5)

        events = [
            {
                "op": "write",
                "path": "/output/result.json",
                "zone_id": "root",
                "agent_id": "agent-noread",
                "agent_generation": 1,
                "metadata": {},
            }
        ]

        _record_lineage_batch(events, session_factory)

        with session_factory() as session:
            svc = LineageService(session)
            from nexus.contracts.urn import NexusURN

            urn = str(NexusURN.for_file("root", "/output/result.json"))
            assert svc.get_lineage(urn) is None

    def test_copy_event_creates_lineage(self, db_engine, session_factory) -> None:
        """Copy events should create lineage with source as upstream."""
        from nexus.factory._lineage_hook import _record_lineage_batch

        registry = AspectRegistry.get()
        if not registry.is_registered("lineage"):
            registry.register("lineage", LineageAspect, max_versions=5)

        events = [
            {
                "op": "copy",
                "path": "/output/copy.json",
                "src_path": "/source/original.json",
                "src_metadata": {"version": 3, "content_id": "src_hash"},
                "zone_id": "root",
                "agent_id": "agent-copy",
                "agent_generation": 1,
                "metadata": {},
            }
        ]

        _record_lineage_batch(events, session_factory)

        with session_factory() as session:
            svc = LineageService(session)
            from nexus.contracts.urn import NexusURN

            urn = str(NexusURN.for_file("root", "/output/copy.json"))
            lineage = svc.get_lineage(urn)
            assert lineage is not None
            assert len(lineage["upstream"]) == 1
            assert lineage["upstream"][0]["path"] == "/source/original.json"
            assert lineage["operation"] == "copy"

    def test_batch_events_individual_savepoints(self, db_engine, session_factory) -> None:
        """Multiple events in a batch — each file gets its own savepoint."""
        from nexus.factory._lineage_hook import _record_lineage_batch
        from nexus.storage.session_read_accumulator import get_accumulator

        registry = AspectRegistry.get()
        if not registry.is_registered("lineage"):
            registry.register("lineage", LineageAspect, max_versions=5)

        acc = get_accumulator()
        acc.record_read("agent-1", 1, "/shared/input.csv", version=1, content_id="e1")
        acc.record_read("agent-2", 1, "/other/data.csv", version=2, content_id="e2")

        events = [
            {
                "op": "write",
                "path": "/out/a.json",
                "zone_id": "root",
                "agent_id": "agent-1",
                "agent_generation": 1,
                "metadata": {},
            },
            {
                "op": "write",
                "path": "/out/b.json",
                "zone_id": "root",
                "agent_id": "agent-2",
                "agent_generation": 1,
                "metadata": {},
            },
        ]

        _record_lineage_batch(events, session_factory)

        with session_factory() as session:
            svc = LineageService(session)
            from nexus.contracts.urn import NexusURN

            urn_a = str(NexusURN.for_file("root", "/out/a.json"))
            urn_b = str(NexusURN.for_file("root", "/out/b.json"))
            assert svc.get_lineage(urn_a) is not None
            assert svc.get_lineage(urn_b) is not None
