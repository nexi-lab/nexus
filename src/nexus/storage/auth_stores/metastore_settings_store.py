"""Metastore-backed implementation of SystemSettingsStoreProtocol.

Stores system settings as FileMetadata entries in the metastore (redb)
under the reserved ``cfg:`` path prefix.

Issue #184: Migrate SystemSettingsModel from RecordStore to Metastore.
"""

from __future__ import annotations

import json

from nexus.contracts.auth_store_types import SystemSettingDTO
from nexus.contracts.metadata import FileMetadata
from nexus.core.metastore import MetastoreABC

_CFG_PREFIX = "cfg:"
_CFG_BACKEND = "_config"


class MetastoreSettingsStore:
    """SystemSettingsStoreProtocol implementation backed by MetastoreABC."""

    def __init__(self, metastore: MetastoreABC) -> None:
        self._metastore = metastore

    def get_setting(self, key: str) -> SystemSettingDTO | None:
        fm = self._metastore.get(f"{_CFG_PREFIX}{key}")
        if fm is None or fm.backend_name != _CFG_BACKEND:
            return None
        payload = json.loads(fm.physical_path)
        return SystemSettingDTO(
            key=key,
            value=payload["v"],
            description=payload.get("d"),
        )

    def set_setting(self, key: str, value: str, *, description: str | None = None) -> None:
        payload = json.dumps({"v": value, "d": description})
        fm = FileMetadata(
            path=f"{_CFG_PREFIX}{key}",
            backend_name=_CFG_BACKEND,
            physical_path=payload,
            size=0,
        )
        self._metastore.put(fm)
