"""Metastore-backed implementation of SystemSettingsStoreProtocol.

Stores system settings as FileMetadata entries in the metastore (redb)
under the reserved ``cfg:`` path prefix.

Issue #184: Migrate SystemSettingsModel from RecordStore to Metastore.

Storage layout
--------------
Each setting reuses the file-metadata KV slot keyed by ``cfg:{key}``.
The ``cfg:`` path prefix uniquely identifies these synthetic records —
no per-record discriminator field is required.  The JSON envelope
``{"v": value, "d": description?}`` is stashed in ``content_id`` (a Nullable
string slot the metastore already round-trips).  Mirrors the pattern
used by :mod:`nexus.bricks.mount.metastore_mount_store`.
"""

from __future__ import annotations

import json
from typing import Any

from nexus.contracts.auth_store_types import SystemSettingDTO
from nexus.contracts.metadata import FileMetadata

_CFG_PREFIX = "cfg:"


class MetastoreSettingsStore:
    """SystemSettingsStoreProtocol implementation backed by the kernel.

    Accepts either a bare ``PyKernel`` (post-W3b factory wiring) or a
    legacy ``RustMetastoreProxy`` shim — the constructor unwraps to the
    kernel handle and dispatches to ``kernel.metastore_*`` directly.
    """

    def __init__(self, metastore: Any) -> None:
        self._metastore = metastore
        self._kernel = (
            metastore if not hasattr(metastore, "_rust_kernel") else metastore._rust_kernel
        )

    def get_setting(self, key: str) -> SystemSettingDTO | None:
        fm = self._kernel.metastore_get(f"{_CFG_PREFIX}{key}")
        if fm is None or not fm.content_id:
            return None
        try:
            payload = json.loads(fm.content_id)
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
        fm = FileMetadata(
            path=f"{_CFG_PREFIX}{key}",
            size=0,
            content_id=json.dumps(payload),
        )
        self._kernel.metastore_put(fm)
