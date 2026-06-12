from sqlalchemy import create_engine, select

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
