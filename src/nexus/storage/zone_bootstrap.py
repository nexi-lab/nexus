"""Default-zone bootstrap shared by every code path that builds the schema.

Issue #3897: every install must contain a usable ``zones.root`` row before
the first ``create_api_key`` call, because that call writes an
``api_key_zones`` junction row whose ``zone_id`` defaults to
``ROOT_ZONE_ID`` and is FK-checked against ``zones.zone_id``.

Two surfaces bootstrap the schema:
- Alembic migration ``eba93656daab`` (production / persistent installs).
- ``SQLAlchemyRecordStore(create_tables=True)`` via
  ``Base.metadata.create_all`` (CLI tooling, tests, ``nexus hub`` flows).

Both must seed the row, otherwise the second path silently leaves a
fresh DB FK-broken — Postgres rejects the first key insert, SQLite may
not even surface the FK if ``PRAGMA foreign_keys`` is off.

This module is the single source of truth. The FastAPI lifespan calls it
as defense-in-depth (covers manually-mutated schemas), and the record
store calls it right after ``create_all``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from nexus.storage.models import ZoneModel

logger = logging.getLogger(__name__)


class _SessionFactory(Protocol):
    def __call__(self) -> "Session": ...


def ensure_root_zone(session_factory: _SessionFactory) -> None:
    """Make sure ``zones.root`` exists and is Active.

    Fails closed:
    - missing row → insert; on IntegrityError (concurrent insert race)
      re-read in a fresh session and require the row to be present;
    - present row → require ``phase == "Active"`` and
      ``deleted_at IS NULL``.

    Anything else raises ``RuntimeError`` so the caller (server startup,
    record-store init) refuses to come up with a broken default zone.

    Args:
        session_factory: callable returning a SQLAlchemy ``Session``
            (e.g. ``SQLAlchemyRecordStore.session_factory`` or
            ``NexusFS.SessionLocal``).
    """
    from datetime import UTC, datetime

    from sqlalchemy.exc import IntegrityError

    from nexus.contracts.constants import ROOT_ZONE_ID
    from nexus.storage.models import ZoneModel

    with session_factory() as session:
        existing = session.get(ZoneModel, ROOT_ZONE_ID)
        if existing is not None:
            _assert_root_zone_active(existing)
            return
        session.add(
            ZoneModel(
                zone_id=ROOT_ZONE_ID,
                name="Root",
                phase="Active",
                finalizers="[]",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        try:
            session.commit()
            logger.info("Seeded default zone %r", ROOT_ZONE_ID)
            return
        except IntegrityError:
            session.rollback()

    # Concurrent insert raced us. Confirm in a fresh session — anything
    # other than a healthy row means the original error wasn't a benign
    # race and we must fail closed.
    with session_factory() as session:
        racer = session.get(ZoneModel, ROOT_ZONE_ID)
        if racer is None:
            raise RuntimeError(
                f"failed to seed default zone {ROOT_ZONE_ID!r}: "
                "IntegrityError on insert and row not visible on re-read"
            )
        _assert_root_zone_active(racer)


def _assert_root_zone_active(zone: "ZoneModel") -> None:
    """Refuse to start when ``zones.root`` exists but isn't Active.

    Auth rejects API keys whose zone is not Active or is soft-deleted, so
    accepting a Terminating/Terminated/deleted root row would let
    bootstrap look healthy while every default agent registration /
    root-token call still failed at request time.
    """
    if zone.phase != "Active" or zone.deleted_at is not None:
        raise RuntimeError(
            f"default zone {zone.zone_id!r} is not usable: "
            f"phase={zone.phase!r} deleted_at={zone.deleted_at!r}. "
            "Restore it to Active (and clear deleted_at) before starting."
        )
