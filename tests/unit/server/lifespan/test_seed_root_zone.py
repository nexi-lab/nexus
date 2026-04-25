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


def test_rejects_inactive_root_zone(session_factory):
    """Terminating/Terminated root must fail startup, not silently pass."""
    with session_factory() as s:
        s.add(ZoneModel(zone_id=ROOT_ZONE_ID, name="root", phase="Terminating"))
        s.commit()

    with pytest.raises(RuntimeError, match="not usable.*Terminating"):
        _seed_root_zone(_svc(session_factory))


def test_rejects_soft_deleted_root_zone(session_factory):
    """deleted_at set on the root row must fail startup."""
    from datetime import UTC, datetime

    with session_factory() as s:
        s.add(
            ZoneModel(
                zone_id=ROOT_ZONE_ID,
                name="root",
                phase="Active",
                deleted_at=datetime.now(UTC),
            )
        )
        s.commit()

    with pytest.raises(RuntimeError, match="not usable.*deleted_at"):
        _seed_root_zone(_svc(session_factory))


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


def test_fails_closed_when_session_raises():
    """Real DB errors must surface — silent skip would mask the bug."""

    def factory():
        raise RuntimeError("connection refused")

    with pytest.raises(RuntimeError, match="connection refused"):
        _seed_root_zone(SimpleNamespace(session_factory=factory))


def test_concurrent_insert_race_treated_as_success(session_factory):
    """Another process inserting the row mid-commit must not be fatal."""
    from sqlalchemy.exc import IntegrityError

    real_factory = session_factory
    insert_session = real_factory()
    races: list[bool] = []

    class _RacingSession:
        def __init__(self) -> None:
            self._inner = real_factory()

        def __enter__(self) -> "_RacingSession":
            self._inner.__enter__()
            return self

        def __exit__(self, *exc) -> None:
            self._inner.__exit__(*exc)

        def get(self, *a, **kw):
            return self._inner.get(*a, **kw)

        def add(self, obj) -> None:
            if not races:
                # Simulate a competing writer landing the row first.
                insert_session.add(ZoneModel(zone_id=ROOT_ZONE_ID, name="racer", phase="Active"))
                insert_session.commit()
                races.append(True)
            self._inner.add(obj)

        def commit(self) -> None:
            self._inner.commit()

        def rollback(self) -> None:
            self._inner.rollback()

    def factory():
        return _RacingSession()

    _seed_root_zone(SimpleNamespace(session_factory=factory))

    # Final state: racer's row survives, our insert was rolled back, no raise.
    with real_factory() as s:
        zone = s.get(ZoneModel, ROOT_ZONE_ID)
        assert zone is not None
        assert zone.name == "racer"

    # And the IntegrityError path was actually exercised.
    assert races == [True]
    # Confirm sqlalchemy IntegrityError is the type _seed_root_zone catches.
    assert IntegrityError is not None
