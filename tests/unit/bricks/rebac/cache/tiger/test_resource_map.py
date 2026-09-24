import os
import uuid

import pytest
from sqlalchemy import create_engine, event, select, text

from nexus.bricks.rebac.cache.tiger.resource_map import TigerResourceMap
from nexus.storage.models import Base
from nexus.storage.models.permissions import TigerResourceMapModel as TRM


def test_bulk_get_or_create_int_ids_inserts_missing_and_returns_existing() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    resource_map = TigerResourceMap(engine)

    with engine.connect() as conn:
        first = resource_map.bulk_get_or_create_int_ids(
            [("file", "/a.txt"), ("file", "/b.txt")],
            conn=conn,
        )
        second = resource_map.bulk_get_or_create_int_ids(
            [("file", "/a.txt"), ("file", "/c.txt")],
            conn=conn,
        )
        rows = conn.execute(
            select(TRM.resource_type, TRM.resource_id).order_by(TRM.resource_int_id)
        ).all()

    assert set(first) == {("file", "/a.txt"), ("file", "/b.txt")}
    assert set(second) == {("file", "/a.txt"), ("file", "/c.txt")}
    assert second[("file", "/a.txt")] == first[("file", "/a.txt")]
    assert {row.resource_id for row in rows} == {"/a.txt", "/b.txt", "/c.txt"}


@pytest.mark.skipif(
    not os.environ.get("NEXUS_TEST_DATABASE_URL"), reason="needs NEXUS_TEST_DATABASE_URL (Postgres)"
)
def test_postgres_bulk_insert_is_one_statement_per_chunk() -> None:
    # The boot-time resource-map sync registers every path in the zone.
    # Passing the rows as executemany params cost one round trip per row
    # (~1.7 s per 500-row chunk on a remote Postgres, ~10 min for 150k
    # paths, stalling concurrent writes); each chunk is now ONE statement.
    engine = create_engine(os.environ["NEXUS_TEST_DATABASE_URL"])
    schema = f"trm_{uuid.uuid4().hex[:8]}"
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = engine.execution_options(schema_translate_map={None: schema})
    try:
        Base.metadata.create_all(engine, tables=[TRM.__table__])
        resource_map = TigerResourceMap(engine)
        executemany_inserts: list[int] = []

        @event.listens_for(engine, "before_cursor_execute")
        def _count(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
            if statement.lstrip().upper().startswith("INSERT") and executemany:
                executemany_inserts.append(len(parameters))

        existing = [("file", f"/pre/{i}") for i in range(10)]
        wanted = existing + [("file", f"/ws/doc-{i}.md") for i in range(2500)]
        with engine.connect() as conn:
            first = resource_map.bulk_get_or_create_int_ids(existing, conn=conn)
            resource_map.clear_cache()
            ids = resource_map.bulk_get_or_create_int_ids(wanted, conn=conn)
            count = conn.execute(select(TRM.resource_int_id)).all()

        assert executemany_inserts == [], "each chunk must be a single multi-row INSERT"
        assert len(ids) == len(wanted) and all(v > 0 for v in ids.values())
        assert all(ids[r] == first[r] for r in existing), "existing ids are preserved"
        assert len(count) == len(wanted)
    finally:
        with create_engine(os.environ["NEXUS_TEST_DATABASE_URL"]).begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
