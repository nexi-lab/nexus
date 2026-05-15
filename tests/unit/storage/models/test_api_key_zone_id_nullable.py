"""APIKeyModel.zone_id is nullable (#3785 F4b — junction is source of truth)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from nexus.storage.models import APIKeyModel
from nexus.storage.models._base import Base


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_zone_id_none_round_trips(session):
    """Admin/zoneless keys can have NULL zone_id — junction carries truth."""
    session.add(
        APIKeyModel(
            key_id="kid_admin",
            key_hash="hash_admin",
            user_id="root",
            name="admin-key",
            zone_id=None,
        )
    )
    session.commit()

    row = session.get(APIKeyModel, "kid_admin")
    assert row is not None
    assert row.zone_id is None


def test_zone_id_omitted_is_null(session):
    """Omitting zone_id stores NULL — no implicit ROOT_ZONE_ID coercion (#3785 F4b)."""
    session.add(
        APIKeyModel(
            key_id="kid_default",
            key_hash="hash_default",
            user_id="bob",
            name="bob-key",
        )
    )
    session.commit()

    row = session.get(APIKeyModel, "kid_default")
    assert row is not None
    assert row.zone_id is None
