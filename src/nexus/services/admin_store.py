"""AdminStoreService — service-layer wrapper for administrative RecordStore queries.

Replaces 12+ direct ORM imports that previously lived in ``nexus.core.nexus_fs``
(violationfix #129).  Per KERNEL-ARCHITECTURE.md §3 the kernel must NOT import
ORM models or execute SQLAlchemy queries directly.

All SQLAlchemy / ORM knowledge is encapsulated here.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


class AdminStoreService:
    """Concrete implementation of :class:`AdminStoreProtocol`.

    Accepts a SQLAlchemy ``sessionmaker`` (from ``RecordStoreABC.session_factory``)
    and encapsulates all ORM access behind plain-Python method signatures.
    """

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    @contextlib.contextmanager
    def _session(self):  # noqa: ANN204
        """Yield a managed session with auto-close."""
        session = self._session_factory()
        try:
            yield session
        finally:
            session.close()

    # ------------------------------------------------------------------
    # API-key helpers
    # ------------------------------------------------------------------

    def get_owner_key_expiration(self, user_id: str) -> datetime | None:
        from sqlalchemy import select

        from nexus.storage.models import APIKeyModel

        with self._session() as session:
            stmt = (
                select(APIKeyModel)
                .where(
                    APIKeyModel.user_id == user_id,
                    APIKeyModel.revoked == 0,
                    APIKeyModel.subject_type != "agent",
                )
                .order_by(APIKeyModel.created_at.desc())
            )
            owner_key = session.scalar(stmt)
            if owner_key and owner_key.expires_at:
                return owner_key.expires_at
            return None

    def get_all_active_agent_keys(self) -> dict[str, dict[str, Any]]:
        from sqlalchemy import select

        from nexus.storage.models import APIKeyModel

        with self._session() as session:
            stmt = select(APIKeyModel).where(
                APIKeyModel.subject_type == "agent",
                APIKeyModel.revoked == 0,
            )
            return {
                key.subject_id: {"inherit_permissions": bool(key.inherit_permissions)}
                for key in session.scalars(stmt).all()
            }

    def get_agent_api_key(self, agent_id: str) -> dict[str, Any] | None:
        from sqlalchemy import select

        from nexus.storage.models import APIKeyModel

        with self._session() as session:
            stmt = select(APIKeyModel).where(
                APIKeyModel.subject_type == "agent",
                APIKeyModel.subject_id == agent_id,
                APIKeyModel.revoked == 0,
            )
            key = session.scalar(stmt)
            if key is None:
                return None
            return {"inherit_permissions": bool(key.inherit_permissions)}

    def revoke_agent_api_keys(self, agent_id: str) -> int:
        from sqlalchemy import update

        from nexus.storage.models import APIKeyModel

        with self._session() as session:
            try:
                stmt = (
                    update(APIKeyModel)
                    .where(
                        APIKeyModel.subject_type == "agent",
                        APIKeyModel.subject_id == agent_id,
                        APIKeyModel.revoked == 0,
                    )
                    .values(revoked=1)
                )
                result = session.execute(stmt)
                session.commit()
                rowcount = result.rowcount if hasattr(result, "rowcount") else 0
                return rowcount
            except Exception:
                session.rollback()
                raise

    def delete_api_keys_for_user(self, user_id: str) -> int:
        from sqlalchemy import delete as sa_delete

        from nexus.storage.models import APIKeyModel

        with self._session() as session:
            try:
                result: Any = session.execute(
                    sa_delete(APIKeyModel).filter_by(user_id=user_id)
                )
                session.commit()
                return result.rowcount
            except Exception:
                session.rollback()
                raise

    # ------------------------------------------------------------------
    # Entity / User / Zone records
    # ------------------------------------------------------------------

    def update_entity_metadata(
        self, entity_type: str, entity_id: str, metadata_json: str
    ) -> None:
        from sqlalchemy import update

        from nexus.storage.models import EntityRegistryModel

        with self._session() as session:
            try:
                stmt = (
                    update(EntityRegistryModel)
                    .where(
                        EntityRegistryModel.entity_type == entity_type,
                        EntityRegistryModel.entity_id == entity_id,
                    )
                    .values(entity_metadata=metadata_json)
                )
                session.execute(stmt)
                session.commit()
            except Exception:
                session.rollback()
                raise

    def provision_zone(self, zone_id: str, zone_name: str | None) -> bool:
        from sqlalchemy import select

        from nexus.storage.models import ZoneModel

        with self._session() as session:
            zone = (
                session.execute(select(ZoneModel).filter_by(zone_id=zone_id))
                .scalars()
                .first()
            )
            if zone:
                return False

            zone = ZoneModel(
                zone_id=zone_id,
                name=zone_name or f"{zone_id} Organization",
                is_active=1,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            session.add(zone)
            session.commit()
            return True

    def provision_user_record(
        self,
        user_id: str,
        email: str,
        display_name: str | None,
        zone_id: str,
    ) -> bool:
        from sqlalchemy import select

        from nexus.storage.models import UserModel

        with self._session() as session:
            user = (
                session.execute(select(UserModel).filter_by(user_id=user_id))
                .scalars()
                .first()
            )
            if user:
                if not user.is_active:
                    user.is_active = 1
                    user.deleted_at = None
                    session.commit()
                    logger.info("Reactivated soft-deleted user: %s", user_id)
                return False

            user = UserModel(
                user_id=user_id,
                email=email,
                username=user_id,
                display_name=display_name or user_id,
                zone_id=zone_id,
                primary_auth_method="api_key",
                is_active=1,
                is_global_admin=0,
                email_verified=1,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            session.add(user)
            session.commit()
            return True

    def lock_user_and_provision_key(
        self,
        user_id: str,
        zone_id: str,
        api_key_creator: Any,
        api_key_name: str,
        expires_at: datetime | None,
    ) -> tuple[str | None, str | None]:
        from sqlalchemy import select

        from nexus.storage.models import APIKeyModel, UserModel

        with self._session() as session:
            user_row = session.execute(
                select(UserModel).where(UserModel.user_id == user_id).with_for_update()
            ).scalar_one_or_none()

            if not user_row:
                raise ValueError(f"User not found: {user_id}")

            existing_key_stmt = (
                select(APIKeyModel)
                .where(
                    APIKeyModel.user_id == user_id,
                    APIKeyModel.subject_type == "user",
                    APIKeyModel.revoked == 0,
                )
                .limit(1)
            )
            existing_key = session.scalar(existing_key_stmt)

            if existing_key:
                return (None, None)

            key_id, api_key = api_key_creator.create_key(
                session,
                user_id=user_id,
                name=api_key_name,
                zone_id=zone_id,
                is_admin=False,
                expires_at=expires_at,
            )
            session.commit()
            return (key_id, api_key)

    def get_user_record(self, user_id: str) -> dict[str, Any] | None:
        from sqlalchemy import select

        from nexus.storage.models import UserModel

        with self._session() as session:
            user = (
                session.execute(select(UserModel).filter_by(user_id=user_id))
                .scalars()
                .first()
            )
            if user is None:
                return None
            return {
                "user_id": user.user_id,
                "email": user.email,
                "zone_id": user.zone_id,
                "is_global_admin": bool(user.is_global_admin),
                "is_active": bool(user.is_active),
            }

    # ------------------------------------------------------------------
    # OAuth cleanup
    # ------------------------------------------------------------------

    def delete_oauth_records(self, user_id: str) -> tuple[int, int]:
        with self._session() as session:
            try:
                from sqlalchemy import delete as sa_delete
                from sqlalchemy import inspect

                from nexus.storage.models import OAuthAPIKeyModel, UserOAuthAccountModel

                has_oauth_tables = False
                if session.bind is not None:
                    inspector = inspect(session.bind)
                    table_names = inspector.get_table_names()
                    has_oauth_tables = (
                        "oauth_api_keys" in table_names
                        and "user_oauth_accounts" in table_names
                    )

                if not has_oauth_tables:
                    return (0, 0)

                oauth_key_result: Any = session.execute(
                    sa_delete(OAuthAPIKeyModel).filter_by(user_id=user_id)
                )
                oauth_acct_result: Any = session.execute(
                    sa_delete(UserOAuthAccountModel).filter_by(user_id=user_id)
                )
                session.commit()
                return (oauth_key_result.rowcount, oauth_acct_result.rowcount)
            except Exception:
                session.rollback()
                raise

    # ------------------------------------------------------------------
    # Metadata / permission cleanup (rmdir cascade)
    # ------------------------------------------------------------------

    def delete_file_paths_by_prefix(self, path_prefix: str) -> int:
        from sqlalchemy import delete as sa_delete

        from nexus.storage.models import FilePathModel

        with self._session() as session:
            try:
                result: Any = session.execute(
                    sa_delete(FilePathModel).where(
                        FilePathModel.virtual_path.like(f"{path_prefix}%")
                    )
                )
                session.commit()
                return result.rowcount
            except Exception:
                session.rollback()
                raise

    def delete_rebac_tuples_by_path(self, path_prefix: str) -> int:
        from sqlalchemy import delete as sa_delete

        from nexus.storage.models import ReBACTupleModel

        with self._session() as session:
            try:
                result: Any = session.execute(
                    sa_delete(ReBACTupleModel).where(
                        ReBACTupleModel.object_type == "file",
                        ReBACTupleModel.object_id.like(f"{path_prefix}%"),
                    )
                )
                session.commit()
                return result.rowcount
            except Exception:
                session.rollback()
                raise

    # ------------------------------------------------------------------
    # User soft-delete
    # ------------------------------------------------------------------

    def soft_delete_user(self, user_id: str) -> bool:
        from sqlalchemy import select

        from nexus.storage.models import UserModel

        with self._session() as session:
            try:
                user = session.execute(
                    select(UserModel).filter_by(user_id=user_id)
                ).scalars().first()
                if user is None:
                    return False
                user.is_active = 0
                user.deleted_at = datetime.now(UTC)
                session.commit()
                return True
            except Exception:
                session.rollback()
                raise

    # ------------------------------------------------------------------
    # Agent key expiration helper
    # ------------------------------------------------------------------

    def get_agent_key_expiration(self, user_id: str) -> datetime:
        expires_at = self.get_owner_key_expiration(user_id)
        now = datetime.now(UTC)

        if expires_at is not None:
            owner_expires = expires_at
            if owner_expires.tzinfo is None:
                owner_expires = owner_expires.replace(tzinfo=UTC)

            if owner_expires > now:
                return owner_expires
            raise ValueError(
                f"Cannot generate API key for agent: Your API key has expired on "
                f"{owner_expires.isoformat()}. Please renew your API key before "
                f"creating agent API keys."
            )

        return now + timedelta(days=365)
