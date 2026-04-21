"""End-to-end test for agent lineage tracking (Issue #3417).

Tests the full flow through the NexusFS kernel:
1. Agent reads multiple files
2. Agent writes an output file
3. Lineage is automatically captured (aspect + reverse index)
4. Queried via LineageService (same path the REST API uses)

Requires: Docker Postgres running (via `nexus up`).
Run: PYTHONPATH=src:tests DATABASE_URL=postgresql://postgres:nexus@localhost:40972/nexus \
     python -m pytest tests/e2e/test_lineage_e2e.py -xvs
"""

import os
import uuid

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID

# Skip if Docker Postgres isn't available
DB_URL = os.environ.get("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not DB_URL or "postgresql" not in DB_URL,
    reason="Requires DATABASE_URL pointing to a running PostgreSQL (nexus up)",
)


@pytest.fixture(scope="module")
def db_url():
    return DB_URL


@pytest.fixture(scope="module")
def engine(db_url):
    from sqlalchemy import create_engine

    eng = create_engine(db_url)
    # Create lineage tables if they don't exist
    from nexus.storage.models._base import Base
    from nexus.storage.models.aspect_store import EntityAspectModel  # noqa: F401
    from nexus.storage.models.lineage_reverse_index import LineageReverseIndexModel  # noqa: F401

    Base.metadata.create_all(eng, checkfirst=True)
    return eng


@pytest.fixture()
def session(engine):
    from sqlalchemy.orm import sessionmaker

    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


@pytest.fixture(autouse=True)
def _reset_accumulator():
    from nexus.storage.session_read_accumulator import reset_accumulator

    reset_accumulator()
    yield
    reset_accumulator()


class TestLineageE2EFlow:
    """Full end-to-end lineage flow using real Postgres."""

    def test_agent_read_write_produces_lineage(self, session) -> None:
        """Simulate: agent reads 3 files, writes 1 output -> lineage recorded."""
        from nexus.contracts.aspects import AspectRegistry, LineageAspect
        from nexus.contracts.urn import NexusURN
        from nexus.storage.lineage_service import LineageService
        from nexus.storage.session_read_accumulator import get_accumulator

        # Ensure lineage aspect is registered
        registry = AspectRegistry.get()
        if not registry.is_registered("lineage"):
            registry.register("lineage", LineageAspect, max_versions=5)

        # --- Setup: unique paths to avoid test interference ---
        test_id = uuid.uuid4().hex[:8]
        agent_id = f"e2e-agent-{test_id}"
        agent_gen = 1
        zone_id = ROOT_ZONE_ID

        input_paths = [
            f"/e2e-test/{test_id}/data/input_a.csv",
            f"/e2e-test/{test_id}/data/input_b.csv",
            f"/e2e-test/{test_id}/config/settings.yaml",
        ]
        output_path = f"/e2e-test/{test_id}/output/result.json"
        output_urn = str(NexusURN.for_file(zone_id, output_path))

        # --- Step 1: Simulate agent reads (accumulator recording) ---
        # Agent must begin a scope first — no default capture.
        acc = get_accumulator()
        acc.begin_scope(agent_id, agent_gen, "main-task")
        for i, path in enumerate(input_paths):
            acc.record_read(
                agent_id,
                agent_gen,
                path,
                version=i + 1,
                etag=f"etag_{test_id}_{i}",
                access_type="content",
            )

        # Verify accumulator has 3 reads
        assert acc.peek(agent_id, agent_gen) == 3

        # --- Step 2: Simulate lineage hook on write ---
        # This is what _record_lineage_batch does:
        reads = acc.consume(agent_id, agent_gen)
        assert len(reads) == 3

        lineage = LineageAspect.from_session_reads(
            reads=reads,
            agent_id=agent_id,
            agent_generation=agent_gen,
            operation="write",
        )

        svc = LineageService(session)
        svc.record_lineage(
            entity_urn=output_urn,
            lineage=lineage,
            zone_id=zone_id,
            downstream_path=output_path,
        )
        session.commit()

        # --- Step 3: Verify lineage aspect (upstream query) ---
        payload = svc.get_lineage(output_urn)
        assert payload is not None, "Lineage aspect should exist"
        assert len(payload["upstream"]) == 3
        assert payload["agent_id"] == agent_id
        assert payload["agent_generation"] == agent_gen
        assert payload["operation"] == "write"

        upstream_paths = {u["path"] for u in payload["upstream"]}
        assert upstream_paths == set(input_paths)

        # --- Step 4: Verify reverse index (downstream query) ---
        for path in input_paths:
            downstream = svc.find_downstream(path, zone_id=zone_id)
            assert len(downstream) >= 1, f"Expected downstream for {path}"
            downstream_urns = {d["downstream_urn"] for d in downstream}
            assert output_urn in downstream_urns

        # --- Step 5: Verify staleness detection ---
        # All outputs should be fresh (versions match)
        for i, path in enumerate(input_paths):
            stale = svc.check_staleness(
                path,
                current_version=i + 1,  # Same version as recorded
                current_etag=f"etag_{test_id}_{i}",
                zone_id=zone_id,
            )
            assert len(stale) == 0, f"Output should NOT be stale for {path}"

        # Change input_a version -> output should be stale
        stale = svc.check_staleness(
            input_paths[0],
            current_version=99,  # Version changed!
            current_etag="new_etag",
            zone_id=zone_id,
        )
        assert len(stale) >= 1, "Output should be stale after input changed"
        assert any(s["downstream_urn"] == output_urn for s in stale)

        print(f"\n✓ Full lineage flow verified for agent {agent_id}")
        print("  - 3 reads accumulated and consumed")
        print("  - Lineage aspect stored with 3 upstream entries")
        print("  - Reverse index: each input maps to output")
        print("  - Staleness detection: fresh when matching, stale when changed")

    def test_explicit_lineage_declaration(self, session) -> None:
        """Test explicit lineage declaration (PUT endpoint path)."""
        from nexus.contracts.aspects import AspectRegistry, LineageAspect
        from nexus.contracts.urn import NexusURN
        from nexus.storage.lineage_service import LineageService

        registry = AspectRegistry.get()
        if not registry.is_registered("lineage"):
            registry.register("lineage", LineageAspect, max_versions=5)

        test_id = uuid.uuid4().hex[:8]
        zone_id = ROOT_ZONE_ID
        output_path = f"/e2e-test/{test_id}/declared_output.json"
        output_urn = str(NexusURN.for_file(zone_id, output_path))

        # Explicit declaration (what PUT /api/v2/lineage/{urn} does)
        upstream = [
            {"path": f"/e2e-test/{test_id}/source_a.txt", "version": 10, "etag": "eA"},
            {"path": f"/e2e-test/{test_id}/source_b.txt", "version": 20, "etag": "eB"},
        ]
        lineage = LineageAspect.from_explicit_declaration(
            upstream=upstream,
            agent_id=f"declared-agent-{test_id}",
            agent_generation=1,
        )

        svc = LineageService(session)
        svc.record_lineage(entity_urn=output_urn, lineage=lineage, zone_id=zone_id)
        session.commit()

        # Verify
        payload = svc.get_lineage(output_urn)
        assert payload is not None
        assert payload["operation"] == "explicit"
        assert len(payload["upstream"]) == 2

        print(f"\n✓ Explicit lineage declaration verified for {output_path}")

    def test_lineage_upsert_replaces_old(self, session) -> None:
        """Test that re-writing a file replaces its lineage (not appends)."""
        from nexus.contracts.aspects import AspectRegistry, LineageAspect
        from nexus.contracts.urn import NexusURN
        from nexus.storage.lineage_service import LineageService

        registry = AspectRegistry.get()
        if not registry.is_registered("lineage"):
            registry.register("lineage", LineageAspect, max_versions=5)

        test_id = uuid.uuid4().hex[:8]
        zone_id = ROOT_ZONE_ID
        output_path = f"/e2e-test/{test_id}/rewritten.json"
        output_urn = str(NexusURN.for_file(zone_id, output_path))
        svc = LineageService(session)

        # First write: reads old_input
        lineage1 = LineageAspect.from_session_reads(
            reads=[{"path": f"/e2e-test/{test_id}/old_input.csv", "version": 1, "etag": "old"}],
            agent_id="agent-upsert",
        )
        svc.record_lineage(entity_urn=output_urn, lineage=lineage1, zone_id=zone_id)
        session.commit()

        old_downstream = svc.find_downstream(f"/e2e-test/{test_id}/old_input.csv", zone_id=zone_id)
        assert len(old_downstream) >= 1

        # Second write: reads new_input instead
        lineage2 = LineageAspect.from_session_reads(
            reads=[{"path": f"/e2e-test/{test_id}/new_input.csv", "version": 5, "etag": "new"}],
            agent_id="agent-upsert",
        )
        svc.record_lineage(entity_urn=output_urn, lineage=lineage2, zone_id=zone_id)
        session.commit()

        # Old input should NO LONGER have this downstream
        old_downstream = svc.find_downstream(f"/e2e-test/{test_id}/old_input.csv", zone_id=zone_id)
        assert not any(d["downstream_urn"] == output_urn for d in old_downstream)

        # New input SHOULD have it
        new_downstream = svc.find_downstream(f"/e2e-test/{test_id}/new_input.csv", zone_id=zone_id)
        assert any(d["downstream_urn"] == output_urn for d in new_downstream)

        print("\n✓ Lineage upsert correctly replaced old entries")

    def test_copy_lineage(self, session) -> None:
        """Test copy operation creates lineage with source as upstream."""
        from nexus.contracts.aspects import AspectRegistry, LineageAspect
        from nexus.contracts.urn import NexusURN
        from nexus.storage.lineage_service import LineageService

        registry = AspectRegistry.get()
        if not registry.is_registered("lineage"):
            registry.register("lineage", LineageAspect, max_versions=5)

        test_id = uuid.uuid4().hex[:8]
        zone_id = ROOT_ZONE_ID
        src_path = f"/e2e-test/{test_id}/original.txt"
        dst_path = f"/e2e-test/{test_id}/copy.txt"
        dst_urn = str(NexusURN.for_file(zone_id, dst_path))

        lineage = LineageAspect.from_explicit_declaration(
            upstream=[{"path": src_path, "version": 3, "etag": "src_hash"}],
            agent_id=f"copy-agent-{test_id}",
        )
        lineage.operation = "copy"

        svc = LineageService(session)
        svc.record_lineage(
            entity_urn=dst_urn, lineage=lineage, zone_id=zone_id, downstream_path=dst_path
        )
        session.commit()

        payload = svc.get_lineage(dst_urn)
        assert payload is not None
        assert payload["operation"] == "copy"
        assert len(payload["upstream"]) == 1
        assert payload["upstream"][0]["path"] == src_path

        downstream = svc.find_downstream(src_path, zone_id=zone_id)
        assert any(d["downstream_urn"] == dst_urn for d in downstream)

        print(f"\n✓ Copy lineage verified: {src_path} -> {dst_path}")

    def test_scoped_read3_write1_write2(self, session) -> None:
        """E2E: read 3 → write 1 → read 2 → write 2, each write gets its own lineage.

        This is the key scenario that motivated scoped tracking.
        Without scopes, write 2 would get NO lineage.
        """
        from nexus.contracts.aspects import AspectRegistry, LineageAspect
        from nexus.contracts.urn import NexusURN
        from nexus.storage.lineage_service import LineageService
        from nexus.storage.session_read_accumulator import get_accumulator

        registry = AspectRegistry.get()
        if not registry.is_registered("lineage"):
            registry.register("lineage", LineageAspect, max_versions=5)

        test_id = uuid.uuid4().hex[:8]
        agent_id = f"e2e-scope-agent-{test_id}"
        agent_gen = 1
        zone_id = ROOT_ZONE_ID
        acc = get_accumulator()

        # --- Task 1: read A, B, C → write output1 ---
        acc.begin_scope(agent_id, agent_gen, "task-1")
        acc.record_read(agent_id, agent_gen, f"/e2e/{test_id}/a.csv", version=1, etag="ea")
        acc.record_read(agent_id, agent_gen, f"/e2e/{test_id}/b.csv", version=2, etag="eb")
        acc.record_read(agent_id, agent_gen, f"/e2e/{test_id}/c.csv", version=3, etag="ec")

        output1_path = f"/e2e/{test_id}/output1.json"
        output1_urn = str(NexusURN.for_file(zone_id, output1_path))

        reads_1 = acc.consume(agent_id, agent_gen, scope_id="task-1")
        assert len(reads_1) == 3

        lineage_1 = LineageAspect.from_session_reads(
            reads=reads_1, agent_id=agent_id, agent_generation=agent_gen, operation="write"
        )
        svc = LineageService(session)
        svc.record_lineage(
            entity_urn=output1_urn, lineage=lineage_1, zone_id=zone_id, downstream_path=output1_path
        )
        session.commit()

        # --- Task 2: read D, E → write output2 ---
        acc.begin_scope(agent_id, agent_gen, "task-2")
        acc.record_read(agent_id, agent_gen, f"/e2e/{test_id}/d.csv", version=4, etag="ed")
        acc.record_read(agent_id, agent_gen, f"/e2e/{test_id}/e.csv", version=5, etag="ee")

        output2_path = f"/e2e/{test_id}/output2.json"
        output2_urn = str(NexusURN.for_file(zone_id, output2_path))

        reads_2 = acc.consume(agent_id, agent_gen, scope_id="task-2")
        assert len(reads_2) == 2

        lineage_2 = LineageAspect.from_session_reads(
            reads=reads_2, agent_id=agent_id, agent_generation=agent_gen, operation="write"
        )
        svc.record_lineage(
            entity_urn=output2_urn, lineage=lineage_2, zone_id=zone_id, downstream_path=output2_path
        )
        session.commit()

        # --- Verify output1 lineage ---
        payload1 = svc.get_lineage(output1_urn)
        assert payload1 is not None
        assert len(payload1["upstream"]) == 3
        paths1 = {u["path"] for u in payload1["upstream"]}
        assert paths1 == {
            f"/e2e/{test_id}/a.csv",
            f"/e2e/{test_id}/b.csv",
            f"/e2e/{test_id}/c.csv",
        }

        # --- Verify output2 lineage (this was the bug: used to be empty) ---
        payload2 = svc.get_lineage(output2_urn)
        assert payload2 is not None, "output2 MUST have lineage (scoped tracking fix)"
        assert len(payload2["upstream"]) == 2
        paths2 = {u["path"] for u in payload2["upstream"]}
        assert paths2 == {
            f"/e2e/{test_id}/d.csv",
            f"/e2e/{test_id}/e.csv",
        }

        # --- Verify reverse index: each input maps to its correct output ---
        ds_a = svc.find_downstream(f"/e2e/{test_id}/a.csv", zone_id=zone_id)
        assert any(d["downstream_urn"] == output1_urn for d in ds_a)
        assert not any(d["downstream_urn"] == output2_urn for d in ds_a)

        ds_d = svc.find_downstream(f"/e2e/{test_id}/d.csv", zone_id=zone_id)
        assert any(d["downstream_urn"] == output2_urn for d in ds_d)
        assert not any(d["downstream_urn"] == output1_urn for d in ds_d)

        # --- Verify staleness: change a.csv → only output1 is stale ---
        stale = svc.check_staleness(
            f"/e2e/{test_id}/a.csv", current_version=99, current_etag="changed", zone_id=zone_id
        )
        assert len(stale) >= 1
        stale_urns = {s["downstream_urn"] for s in stale}
        assert output1_urn in stale_urns
        assert output2_urn not in stale_urns

        print(f"\n✓ Scoped lineage verified for agent {agent_id}")
        print("  - Task 1: read A,B,C → write output1 → lineage = [A,B,C]")
        print("  - Task 2: read D,E → write output2 → lineage = [D,E]")
        print("  - Reverse index correctly isolates inputs per output")
        print("  - Staleness correctly scoped: changing A only flags output1")
