"""Metastore-backed implementation of SystemSettingsStoreProtocol.

Stores system settings in the metastore (redb) via kernel syscalls.

Issue #184: Migrate SystemSettingsModel from RecordStore to Metastore.

Storage layout
--------------
Each setting is accessed through the ``/__sys__/cfg/{key}`` virtual path.
The kernel intercepts this prefix in sys_stat/sys_write and maps it to
the metastore entry ``cfg:{key}``.  The JSON envelope
``{"v": value, "d": description?}`` is stored in the ``content_id`` field.
Service layer uses sys_stat to read and sys_setattr to write — no
raw metastore access.
"""

from __future__ import annotations

import json
from typing import Any

from nexus.contracts.auth_store_types import SystemSettingDTO
from nexus.contracts.constants import ROOT_ZONE_ID

_CFG_PREFIX = "/__sys__/cfg/"


class MetastoreSettingsStore:
    """SystemSettingsStoreProtocol implementation backed by the kernel.

    Accepts either a bare ``PyKernel`` (post-W3b factory wiring) or a
    legacy ``RustMetastoreProxy`` shim — the constructor unwraps to the
    kernel handle and dispatches to ``kernel.metastore_*`` directly.
    """

    def __init__(self, metastore: Any) -> None:
        self._metastore = metastore
        self._kernel = metastore

    def get_setting(self, key: str) -> SystemSettingDTO | None:
        stat = self._kernel.sys_stat(f"{_CFG_PREFIX}{key}", ROOT_ZONE_ID)
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
        json_bytes = json.dumps(payload).encode("utf-8")
        ctx = {"user_id": "system", "zone_id": ROOT_ZONE_ID, "is_admin": True}
        self._kernel.sys_write(f"{_CFG_PREFIX}{key}", ctx, json_bytes, 0)
