"""Issue #3897 — startup seeds the default root zone.

Without ``zones.root``, the first call to ``create_api_key`` (e.g. via
``POST /api/v2/agents/register``) fails with FK
``api_key_zones_zone_id_fkey``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.server.lifespan.permissions import _seed_root_zone
from nexus.storage.api_key_ops import create_api_key
from nexus.storage.models import ZoneModel
from nexus.storage.models._base import Base


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _svc(session_factory) -> SimpleNamespace:
    return SimpleNamespace(session_factory=session_factory)


def test_seeds_root_zone_when_missing(session_factory):
    _seed_root_zone(_svc(session_factory))

    with session_factory() as s:
        zone = s.get(ZoneModel, ROOT_ZONE_ID)
        assert zone is not None
        assert zone.phase == "Active"


def test_idempotent_when_root_already_exists(session_factory):
    with session_factory() as s:
        s.add(ZoneModel(zone_id=ROOT_ZONE_ID, name="preexisting", phase="Active"))
        s.commit()

    _seed_root_zone(_svc(session_factory))

    with session_factory() as s:
        zone = s.get(ZoneModel, ROOT_ZONE_ID)
        assert zone is not None
        assert zone.name == "preexisting"  # untouched


def test_noop_without_session_factory():
    _seed_root_zone(SimpleNamespace(session_factory=None))


def test_create_api_key_succeeds_after_seed(session_factory):
    """Regression for #3897: FK violation on first agent register."""
    _seed_root_zone(_svc(session_factory))

    with session_factory() as s:
        # subject_id="admin" mirrors the synthetic NEXUS_API_KEY admin
        # (server/dependencies.py) used by /api/v2/agents/register.
        create_api_key(
            s,
            user_id="admin",
            name="agent:test",
            subject_type="agent",
            subject_id="agent-test",
            zone_id=ROOT_ZONE_ID,
        )
        s.commit()
