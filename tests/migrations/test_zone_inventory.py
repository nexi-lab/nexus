"""§10.2 inventory drill — the report is the deliverable.

Constructs each legacy state shape directly in a throwaway SQLite store
(inventory input IS legacy data — constructing it here is the fixture, not
an E2E shortcut) and asserts the nine-bucket classification plus the
report-only contract: nothing is adopted, deleted, or renamed.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.contracts.zone_v1 import _projection_validator
from nexus.storage.models import Base, ZoneModel
from nexus.storage.zone_migration import MOSS_SIDE_INVENTORY_CLASSES, zone_inventory


def _validator(value: str) -> None:
    # Owner projection (existing-zone-id-ref): raises on illegal historical ids.
    _projection_validator("existing-zone-id-ref.schema.gen.json").validate(value)


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        yield s


def test_inventory_classifies_all_nine_buckets_and_touches_nothing(session) -> None:
    consistent = "inv-consistent"
    sql_only = "inv-sql-only"
    terminated_live = "inv-terminated-live"
    illegal = "inv_ILLEGAL_uppercase"

    for zone_id, status in (
        (consistent, "active"),
        (sql_only, "active"),
        (terminated_live, "deleted"),
    ):
        session.add(
            ZoneModel(
                zone_id=zone_id,
                name=zone_id,
                canonical_status=status,
            )
        )
    session.commit()

    runtime_ids = frozenset({consistent, terminated_live, "inv-runtime-only"})

    report = zone_inventory(
        session,
        runtime_zone_ids=runtime_ids,
        api_key_zone_attributions={
            ("key-unknown", consistent): {
                "subject": True,
                "issuer": False,
                "permissions": True,
                "source": True,
            },
            ("key-known", consistent): {
                "subject": True,
                "issuer": True,
                "permissions": True,
                "source": True,
            },
        },
        zone_id_validator=_validator,
    )

    assert report.items["zone_sql_and_runtime_consistent"] == (consistent,)
    assert report.items["zone_sql_only"] == (sql_only,)
    assert report.items["zone_runtime_only"] == ("inv-runtime-only",)
    assert report.items["zone_terminated_sql_live_runtime"] == (terminated_live,)
    assert (
        report.items["zone_illegal_historical_id"] == (illegal,)
        or report.items["zone_illegal_historical_id"] == ()
    )
    # The illegal id was never inserted above (it fails the model layer), so
    # the bucket is exercised via direct classification in the second test.
    assert report.items["api_key_zone_unattributable"] == ("key-unknown",)
    assert report.counts["api_key_zone_unattributable"] == 1
    assert report.moss_side == MOSS_SIDE_INVENTORY_CLASSES
    for cls in MOSS_SIDE_INVENTORY_CLASSES:
        assert cls not in report.items or report.items[cls] == ()

    # Report-only: the stored state is untouched by the scan.
    remaining = {row.zone_id for row in session.query(ZoneModel).all()}
    assert remaining == {consistent, sql_only, terminated_live}


def test_inventory_flags_illegal_historical_id_when_present(session) -> None:
    # An illegal historical id can exist in legacy rows (the whole point of
    # the class): insert it bypassing the model validators via raw SQL.
    session.execute(
        ZoneModel.__table__.insert().values(
            zone_id="Inv_Legacy_ID",
            name="legacy",
            canonical_status="active",
        )
    )
    session.commit()
    report = zone_inventory(session, runtime_zone_ids=frozenset(), zone_id_validator=_validator)
    assert report.items["zone_illegal_historical_id"] == ("Inv_Legacy_ID",)
    assert report.items["zone_sql_only"] == ()


def test_inventory_with_grantless_state_reports_empty_buckets(session) -> None:
    report = zone_inventory(session, runtime_zone_ids=frozenset(), zone_id_validator=_validator)
    assert all(count == 0 for count in report.counts.values())
