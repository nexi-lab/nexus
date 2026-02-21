"""SQLAlchemy implementation of SystemSettingsStoreProtocol.

Issue #2436: Decouples auth brick from direct ORM model imports.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from nexus.contracts.auth_store_types import SystemSettingDTO
from nexus.storage.models import SystemSettingsModel


def _to_dto(setting: SystemSettingsModel) -> SystemSettingDTO:
    return SystemSettingDTO(
        key=setting.key,
        value=setting.value,
        description=setting.description,
    )


class SQLAlchemySettingsStore:
    """SystemSettingsStoreProtocol implementation backed by SQLAlchemy."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def get_setting(self, key: str) -> SystemSettingDTO | None:
        with self._session_factory() as session:
            setting = session.execute(
                select(SystemSettingsModel).where(SystemSettingsModel.key == key)
            ).scalar_one_or_none()
            return _to_dto(setting) if setting else None

    def set_setting(self, key: str, value: str, *, description: str | None = None) -> None:
        with self._session_factory() as session:
            existing = session.execute(
                select(SystemSettingsModel).where(SystemSettingsModel.key == key)
            ).scalar_one_or_none()
            if existing:
                existing.value = value
                if description is not None:
                    existing.description = description
            else:
                setting = SystemSettingsModel(
                    key=key,
                    value=value,
                    description=description,
                )
                session.add(setting)
            session.commit()
