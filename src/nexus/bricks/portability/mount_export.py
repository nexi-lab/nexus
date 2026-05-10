"""Mount-config export for .nexus bundles (Issue #4083).

Pulls mount configurations from MountManager, runs each through the redaction
contract (declared via ConnectionArg.secret=True or audit_safe=True), and writes
a sorted JSONL file alongside the rest of the bundle. Audit failures abort the
export with SensitiveFieldNotDeclaredError before any bytes are written.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from nexus.bricks.portability.models import MountRecord, PlaceholderRef
from nexus.bricks.portability.redaction import redact_config


def collect_mounts(
    mount_manager: Any,
    *,
    zone_id: str | None,
) -> list[dict[str, Any]]:
    """Return raw mount-config dicts from mount_manager (zone-filtered if zone_id set).

    mount_manager must expose ``list_mounts(zone_id=...)`` returning a list of
    dicts with keys: mount_id, mount_point, backend_type, backend_config,
    owner_user_id, zone_id, description.  Passed in by the caller (DI) so the
    portability brick has no compile-time coupling to nexus.bricks.mount.
    """
    return cast(list[dict[str, Any]], mount_manager.list_mounts(zone_id=zone_id))


def redact_and_write(
    mounts: list[dict[str, Any]],
    *,
    out_path: Path,
) -> list[PlaceholderRef]:
    """Per-mount: run redaction, write JSONL line; return aggregated placeholders.

    Lines are sorted by mount_id for byte-stable output. Each line is canonical
    JSON (sort_keys=True, no extra whitespace). On audit failure for any mount,
    raises SensitiveFieldNotDeclaredError before writing anything.
    """
    sorted_mounts = sorted(mounts, key=lambda m: m["mount_id"])

    # Phase 1: redact in memory, surface audit failures before any I/O.
    redacted_records: list[MountRecord] = []
    placeholders: list[PlaceholderRef] = []
    for raw in sorted_mounts:
        redacted_config, mount_phs = redact_config(
            backend_type=raw["backend_type"],
            config=raw.get("backend_config", {}) or {},
            mount_id=raw["mount_id"],
        )
        record = MountRecord(
            mount_id=raw["mount_id"],
            mount_point=raw["mount_point"],
            backend_type=raw["backend_type"],
            backend_config=redacted_config,
            owner_user_id=raw.get("owner_user_id"),
            zone_id=raw.get("zone_id"),
            description=raw.get("description"),
        )
        redacted_records.append(record)
        placeholders.extend(mount_phs)

    # Phase 2: write JSONL
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not redacted_records:
        out_path.write_text("")
    else:
        with out_path.open("w") as fh:
            for record in redacted_records:
                fh.write(json.dumps(record.to_dict(), sort_keys=True))
                fh.write("\n")

    return placeholders


__all__ = ["collect_mounts", "redact_and_write"]
