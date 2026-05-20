"""Metastore-backed implementation of SystemSettingsStoreProtocol.

Stores system settings in the metastore (redb) via kernel syscalls.

Issue #184: Migrate SystemSettingsModel from RecordStore to Metastore.

Storage layout
--------------
Each setting is stored at the VFS path ``/settings/{key}`` in the global
(root-zone) metastore.  No kernel special-casing — ``/settings/`` is a
legitimate VFS namespace that routes through standard syscalls.
The JSON envelope ``{"v": value, "d": description?}`` is stored in the
``content_id`` field.  Service layer uses sys_stat to read and sys_setattr
to write (DT_REG upsert with content_id).
"""

from __future__ import annotations

import json
from typing import Any

from nexus.contracts.auth_store_types import SystemSettingDTO

_CFG_PREFIX = "/settings/"


class MetastoreSettingsStore:
    """SystemSettingsStoreProtocol implementation backed by the kernel.

    Accepts a ``NexusFS`` (or any object exposing ``sys_stat`` / ``sys_setattr``)
    and routes through the public syscall API.  Settings are stored at
    ``/settings/{key}`` in the root zone — no zone_id needed since the
    kernel resolves root-zone paths by default.
    """

    def __init__(self, nexus_fs: Any) -> None:
        self._fs = nexus_fs

    def get_setting(self, key: str) -> SystemSettingDTO | None:
        stat = self._fs.sys_stat(f"{_CFG_PREFIX}{key}")
        if stat is None or not stat.get("content_id"):
            return None
        try:
            payload = json.loads(stat["content_id"])
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict) or "v" not in payload:
            return None
        return SystemSettingDTO(
            key=key,
            value=payload["v"],
            description=payload.get("d"),
        )

    def set_setting(
        self,
        key: str,
        value: str,
        *,
        description: str | None = None,
    ) -> None:
        payload: dict[str, str | None] = {"v": value}
        if description is not None:
            payload["d"] = description
        json_payload = json.dumps(payload)
        self._fs.sys_setattr(
            f"{_CFG_PREFIX}{key}",
            entry_type=0,  # DT_REG upsert
            content_id=json_payload,
            size=0,
        )
