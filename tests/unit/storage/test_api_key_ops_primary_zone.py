"""Unit tests for get_primary_zone and get_primary_zones_for_keys (#3871)."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from nexus.storage.api_key_ops import (
    create_api_key,
    get_primary_zone,
    get_primary_zones_for_keys,
)
from nexus.storage.models._base import Base
from nexus.storage.models.auth import APIKeyZoneModel, ZoneModel


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for zid in ("eng", "ops", "legal"):
            s.add(ZoneModel(zone_id=zid, name=zid, phase="Active"))
        s.commit()
        yield s


def test_get_primary_zone_returns_none_for_zoneless_key(session):
    # `zones=[]` raises ValueError; zoneless = omit both `zones` and `zone_id`.
    key_id, _ = create_api_key(session, user_id="u1", name="admin", is_admin=True)
    assert get_primary_zone(session, key_id) is None


def test_get_primary_zone_returns_only_zone_for_single_zone_key(session):
    key_id, _ = create_api_key(session, user_id="u1", name="alice", zones=["eng"])
    assert get_primary_zone(session, key_id) == "eng"


def test_get_primary_zone_returns_min_granted_at(session):
    key_id, _ = create_api_key(session, user_id="u1", name="multi", zones=["eng"])
    later = dt.datetime.now(dt.UTC).replace(tzinfo=None) + dt.timedelta(seconds=10)
    session.add(APIKeyZoneModel(key_id=key_id, zone_id="ops", granted_at=later, permissions="rw"))
    session.commit()
    assert get_primary_zone(session, key_id) == "eng"


def test_get_primary_zone_tiebreaker_is_zone_id_asc(session):
    # Create zoneless then add two junction rows by hand with identical granted_at.
    key_id, _ = create_api_key(session, user_id="u1", name="tied", is_admin=True)
    same = dt.datetime(2026, 4, 25, 12, 0, 0)
    session.add(APIKeyZoneModel(key_id=key_id, zone_id="ops", granted_at=same, permissions="rw"))
    session.add(APIKeyZoneModel(key_id=key_id, zone_id="eng", granted_at=same, permissions="rw"))
    session.commit()
    assert get_primary_zone(session, key_id) == "eng"


def test_get_primary_zones_for_keys_empty_input(session):
    assert get_primary_zones_for_keys(session, []) == {}


def test_get_primary_zones_for_keys_batch(session):
    a, _ = create_api_key(session, user_id="u1", name="a", zones=["eng"])
    b, _ = create_api_key(session, user_id="u1", name="b", zones=["ops"])
    c, _ = create_api_key(session, user_id="u1", name="c", is_admin=True)  # zoneless
    result = get_primary_zones_for_keys(session, [a, b, c])
    assert result == {a: "eng", b: "ops"}  # c absent (zoneless)


def test_get_primary_zones_for_keys_single_query(session):
    from sqlalchemy import event

    a, _ = create_api_key(session, user_id="u1", name="a", zones=["eng"])
    b, _ = create_api_key(session, user_id="u1", name="b", zones=["ops"])
    seen: list[str] = []

    @event.listens_for(session.bind, "before_cursor_execute")
    def _capture(conn, cursor, statement, *_):  # noqa: ARG001
        seen.append(statement)

    result = get_primary_zones_for_keys(session, [a, b])
    # Correctness: result must be right (catches "1 query, wrong shape").
    assert result == {a: "eng", b: "ops"}
    # Efficiency: exactly one SELECT query touched the junction.
    junction_stmts = [
        s for s in seen if "api_key_zones" in s and s.strip().upper().startswith("SELECT")
    ]
    assert len(junction_stmts) == 1
    # Shape: server did the filtering + primary selection (catches
    # "1 query that pulls every row and filters in Python").
    sql = junction_stmts[0].lower()
    assert " in (" in sql, "expected server-side key_id IN (...) filter"
    assert "row_number" in sql, "expected window-function primary selection"
