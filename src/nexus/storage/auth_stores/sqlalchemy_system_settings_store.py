"""SQLAlchemy implementation of ``SystemSettingsStoreProtocol``.

Stores application-level key/value settings in the ``system_settings``
RecordStore table (one of the Four Storage Pillars — *services-only*).
This is the canonical home for all application settings including the
OAuth encryption key; earlier implementations that persisted settings
into the filesystem metastore (``cfg:`` prefix on ``FileMetadata``)
violated the LEGO tier boundary between filesystem-level and
application-level SSOTs.

Pairs with ``RecordStoreABC`` — takes a session factory, not a
Metastore — so the OAuth key lives alongside other app-level secrets
(``secret_store``, ``secrets_audit_log``, ``oauth_credentials``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexus.contracts.auth_store_types import SystemSettingDTO
from nexus.storage.models import SystemSettingsModel

logger = logging.getLogger(__name__)


def _to_dto(row: SystemSettingsModel) -> SystemSettingDTO:
    return SystemSettingDTO(
        key=row.key,
        value=row.value,
        description=row.description,
    )


class SQLAlchemySystemSettingsStore:
    """``SystemSettingsStoreProtocol`` backed by the ``system_settings`` table.

    Upserts via read-then-insert/update within a single transaction — the
    primary key is ``key``, so SQLite / PostgreSQL both converge on the
    same row under concurrent writers. Callers get last-writer-wins; no
    optimistic-concurrency token because settings are infrequently
    updated (OAuth key once per install).
    """

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def get_setting(self, key: str) -> SystemSettingDTO | None:
        with self._session_factory() as session:
            row = session.execute(
                select(SystemSettingsModel).where(SystemSettingsModel.key == key)
            ).scalar_one_or_none()
            if row is None:
                return None
            return _to_dto(row)

    def set_setting(self, key: str, value: str, *, description: str | None = None) -> None:
        with self._session_factory() as session:
            row = session.execute(
                select(SystemSettingsModel).where(SystemSettingsModel.key == key)
            ).scalar_one_or_none()
            if row is None:
                session.add(SystemSettingsModel(key=key, value=value, description=description))
            else:
                row.value = value
                if description is not None:
                    row.description = description
            # ``TimestampMixin`` handles created_at default + updated_at onupdate.
            session.commit()
