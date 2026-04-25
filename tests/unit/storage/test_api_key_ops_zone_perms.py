"""Per-zone permissions in api_key_ops (#3785)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.storage.api_key_ops import (
    add_zone_to_key,
    create_api_key,
    get_zone_perms_for_key,
)
from nexus.storage.models import Base


def _make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    return Session


def _create_and_read(session, **kwargs):
    key_id, _ = create_api_key(session, **kwargs)
    return sorted(get_zone_perms_for_key(session, key_id))


def test_bare_zone_string_defaults_to_rw():
    Session = _make_session()
    with Session() as s, s.begin():
        result = _create_and_read(s, user_id="alice", name="t1", zones=["eng"])
    assert result == [("eng", "rw")]


def test_tuple_form_round_trips():
    Session = _make_session()
    with Session() as s, s.begin():
        result = _create_and_read(
            s, user_id="alice", name="t2", zones=[("eng", "r"), ("ops", "rwx")]
        )
    assert result == [("eng", "r"), ("ops", "rwx")]


def test_mixed_string_and_tuple():
    Session = _make_session()
    with Session() as s, s.begin():
        result = _create_and_read(s, user_id="alice", name="t3", zones=["eng", ("ops", "r")])
    assert result == [("eng", "rw"), ("ops", "r")]


def test_invalid_perms_string_raises_in_create():
    Session = _make_session()
    with Session() as s, s.begin():  # noqa: SIM117
        with pytest.raises(ValueError, match="invalid permissions 'badperm'"):
            create_api_key(s, user_id="alice", name="t4", zones=[("eng", "badperm")])


def test_add_zone_to_key_round_trips_perms():
    Session = _make_session()
    with Session() as s, s.begin():
        key_id, _ = create_api_key(s, user_id="alice", name="t5", zones=["eng"])
    with Session() as s, s.begin():
        added = add_zone_to_key(s, key_id, "ops", permissions="r")
        assert added is True
    with Session() as s:
        pairs = sorted(get_zone_perms_for_key(s, key_id))
    assert pairs == [("eng", "rw"), ("ops", "r")]


def test_add_zone_to_key_rejects_invalid_perms():
    Session = _make_session()
    with Session() as s, s.begin():
        key_id, _ = create_api_key(s, user_id="alice", name="t6", zones=["eng"])
    with Session() as s, s.begin():  # noqa: SIM117
        with pytest.raises(ValueError, match="invalid permissions 'badperms'"):
            add_zone_to_key(s, key_id, "ops", permissions="badperms")
