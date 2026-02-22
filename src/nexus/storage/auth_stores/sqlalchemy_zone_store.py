"""SQLAlchemy implementation of ZoneStoreProtocol.

Issue #2436: Decouples auth brick from direct ORM model imports.
"""

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from nexus.contracts.auth_store_types import ZoneDTO
from nexus.storage.models import ZoneModel


def _to_dto(zone: ZoneModel) -> ZoneDTO:
    return ZoneDTO(
        zone_id=zone.zone_id,
        name=zone.name,
        domain=zone.domain,
        description=zone.description,
        phase=zone.phase,
        created_at=zone.created_at,
        updated_at=zone.updated_at,
    )


class SQLAlchemyZoneStore:
    """ZoneStoreProtocol implementation backed by SQLAlchemy."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def create_zone(
        self,
        *,
        zone_id: str,
        name: str,
        domain: str | None = None,
        description: str | None = None,
        settings: str | None = None,
    ) -> ZoneDTO:
        now = datetime.now(UTC)
        zone = ZoneModel(
            zone_id=zone_id,
            name=name,
            domain=domain,
            description=description,
            settings=settings,
            phase="Active",
            finalizers="[]",
            created_at=now,
            updated_at=now,
        )
        with self._session_factory() as session:
            session.add(zone)
            session.commit()
            session.refresh(zone)
            return _to_dto(zone)

    def get_zone(self, zone_id: str) -> ZoneDTO | None:
        with self._session_factory() as session:
            zone = session.get(ZoneModel, zone_id)
            return _to_dto(zone) if zone else None

    def zone_exists(self, zone_id: str) -> bool:
        with self._session_factory() as session:
            return session.get(ZoneModel, zone_id) is not None
