"""SQLAlchemy implementation of APIKeyStoreProtocol.

Issue #2436: Decouples auth brick from direct ORM model imports.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from nexus.contracts.auth_store_types import APIKeyDTO
from nexus.storage.models import APIKeyModel, APIKeyZoneModel

logger = logging.getLogger(__name__)


def _to_dto(key: APIKeyModel) -> APIKeyDTO:
    return APIKeyDTO(
        key_id=key.key_id,
        key_hash=key.key_hash,
        user_id=key.user_id,
        name=key.name,
        subject_type=key.subject_type or "user",
        subject_id=key.subject_id,
        zone_id=key.zone_id,
        is_admin=bool(key.is_admin),
        expires_at=key.expires_at,
        revoked=bool(key.revoked),
        inherit_permissions=bool(key.inherit_permissions),
        last_used_at=key.last_used_at,
        created_at=key.created_at,
    )


class SQLAlchemyAPIKeyStore:
    """APIKeyStoreProtocol implementation backed by SQLAlchemy."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def create_key(
        self,
        *,
        key_hash: str,
        user_id: str,
        name: str,
        subject_type: str = "user",
        subject_id: str | None = None,
        zone_id: str | None = None,
        is_admin: bool = False,
        expires_at: datetime | None = None,
        inherit_permissions: bool = False,
    ) -> APIKeyDTO:
        key = APIKeyModel(
            key_hash=key_hash,
            user_id=user_id,
            name=name,
            subject_type=subject_type,
            subject_id=subject_id or user_id,
            zone_id=None,
            is_admin=int(is_admin),
            expires_at=expires_at,
            inherit_permissions=int(inherit_permissions),
        )
        # #3871 round 4: non-admin keys must have a zone (see Phase 2 docs).
        if not zone_id and not is_admin:
            raise ValueError(
                "SQLAlchemyAPIKeyStore.create_key: non-admin keys must specify a zone_id "
                "(zoneless tokens are reserved for global admins, #3871)"
            )

        with self._session_factory() as session:
            # #3871 round 3+6: validate zone exists, is Active, and not
            # deleted before junction insert. Round 6 also rejects
            # Terminating/soft-deleted zones — otherwise the token mints
            # successfully but the lifecycle gate rejects at first auth.
            if zone_id:
                from nexus.storage.models import ZoneModel

                zone = session.scalar(select(ZoneModel).where(ZoneModel.zone_id == zone_id))
                if zone is None or zone.phase != "Active" or zone.deleted_at is not None:
                    raise ValueError(
                        f"SQLAlchemyAPIKeyStore.create_key: zone {zone_id!r} is not active "
                        "(missing, Terminating, or soft-deleted); create or restore "
                        "the zone before issuing keys against it"
                    )

            session.add(key)
            session.flush()  # populate key.key_id before junction insert
            if zone_id:  # non-empty zone_id → populate junction
                session.add(APIKeyZoneModel(key_id=key.key_id, zone_id=zone_id, permissions="rw"))
            session.commit()
            session.refresh(key)
            return _to_dto(key)

    def get_by_hash(self, key_hash: str) -> APIKeyDTO | None:
        from sqlalchemy import or_

        now = datetime.now(UTC)
        with self._session_factory() as session:
            key = session.scalar(
                select(APIKeyModel).where(
                    APIKeyModel.key_hash == key_hash,
                    APIKeyModel.revoked == 0,
                    or_(
                        APIKeyModel.expires_at.is_(None),
                        APIKeyModel.expires_at > now,
                    ),
                )
            )
            return _to_dto(key) if key else None

    def revoke_key(self, key_id: str, *, zone_id: str | None = None) -> bool:
        """Revoke an API key.

        Zone access filter — when provided, only revokes keys that grant access
        to this zone via the api_key_zones junction (matches multi-zone keys on
        every granted zone, not only primary). #3871.
        """
        with self._session_factory() as session:
            stmt = select(APIKeyModel).where(APIKeyModel.key_id == key_id)
            if zone_id is not None:
                stmt = stmt.join(
                    APIKeyZoneModel, APIKeyZoneModel.key_id == APIKeyModel.key_id
                ).where(APIKeyZoneModel.zone_id == zone_id)
            key = session.scalar(stmt)
            if not key:
                return False
            # Snapshot subject before mutation — needed to drop the cached
            # zone_perms so revoked grants can't authorise further requests
            # on the native (Rust-stripped) code path.  Issue #3786 / Codex
            # Round 4 finding #3.
            revoked_subject = key.subject_id if hasattr(key, "subject_id") else key.user_id
            key.revoked = 1
            key.revoked_at = datetime.now(UTC)
            session.commit()

            if revoked_subject:
                try:
                    from nexus.lib.zone_perms_cache import invalidate_zone_perms

                    invalidate_zone_perms(revoked_subject)
                except Exception:
                    logger.warning(
                        "Failed to invalidate zone_perms cache for revoked key %s",
                        key_id,
                        exc_info=True,
                    )
            return True

    def update_last_used(self, key_hash: str) -> None:
        try:
            with self._session_factory() as session:
                session.execute(
                    update(APIKeyModel)
                    .where(APIKeyModel.key_hash == key_hash)
                    .values(last_used_at=datetime.now(UTC))
                )
                session.commit()
        except (OperationalError, ProgrammingError):
            logger.warning("Failed to update last_used_at (non-critical)", exc_info=True)
