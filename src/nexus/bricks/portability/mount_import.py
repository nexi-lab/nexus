"""Mount-config import for .nexus bundles (Issue #4083).

Reads mounts.jsonl, validates that all redacted fields have overrides supplied,
re-injects values, and restores via MountManager. Validation runs *before* any
backend init or persistence — operators see every credential gap in one error.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Protocol

from nexus.bricks.portability.models import (
    ConflictMode,
    ImportError,
    MissingCredentialsError,
    MountRecord,
)

_PLACEHOLDER_RE = re.compile(r"^\$\{MOUNT_(?P<id>.+)_(?P<field>[A-Z0-9_]+)\}$")
"""Matches ${MOUNT_<id>_<FIELD>} where <id> may contain underscores and <field>
is the upper-cased CONNECTION_ARGS key. Greedy on <id> is safe because <field>
is constrained to [A-Z0-9_]+ at the suffix."""


class _MountWriter(Protocol):
    """Narrow contract for the MountManager surface mount_import needs.

    Defined locally so the brick boundary check (which forbids cross-brick type
    imports) is satisfied while preserving static typing on call sites.
    """

    def get_mount(self, mount_point: str) -> dict[str, Any] | None: ...

    def save_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        owner_user_id: str | None = None,
        zone_id: str | None = None,
        description: str | None = None,
    ) -> str: ...

    def update_mount(
        self,
        mount_point: str,
        backend_config: dict[str, Any] | None = None,
        description: str | None = None,
    ) -> bool: ...


def read_mounts(bundle_dir: Path) -> list[MountRecord]:
    """Parse bundle_dir/mounts.jsonl. Returns [] if the file is absent."""
    path = bundle_dir / "mounts.jsonl"
    if not path.exists():
        return []
    out: list[MountRecord] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(MountRecord.from_dict(json.loads(line)))
    return out


def _redacted_fields(record: MountRecord) -> list[str]:
    """Return the list of CONNECTION_ARGS keys whose value is a placeholder string."""
    return [
        name
        for name, value in record.backend_config.items()
        if isinstance(value, str) and _PLACEHOLDER_RE.match(value)
    ]


def validate_overrides(
    mounts: list[MountRecord],
    overrides: dict[str, dict[str, str]] | None,
) -> None:
    """Walk every redacted field; raise MissingCredentialsError listing all gaps.

    Pure function: no side effects. Runs before any backend init.
    """
    overrides = overrides or {}
    missing: dict[str, list[str]] = {}
    for record in mounts:
        provided = overrides.get(record.mount_id, {}) or {}
        gaps = [f for f in _redacted_fields(record) if f not in provided]
        if gaps:
            missing[record.mount_id] = sorted(gaps)
    if missing:
        raise MissingCredentialsError(missing=missing)


def materialize(
    mount_record: MountRecord,
    overrides_for_mount: dict[str, str],
) -> dict[str, Any]:
    """Substitute ${MOUNT_<id>_<FIELD>} placeholders. Returns the final backend_config."""
    out = dict(mount_record.backend_config)
    for field_name, value in list(out.items()):
        if (
            isinstance(value, str)
            and _PLACEHOLDER_RE.match(value)
            and field_name in overrides_for_mount
        ):
            out[field_name] = overrides_for_mount[field_name]
    return out


def import_mounts(
    mounts: list[MountRecord],
    overrides: dict[str, dict[str, str]],
    mount_manager: _MountWriter,
    *,
    target_zone_id: str | None,
    conflict_mode: ConflictMode,
) -> list[ImportError]:
    """Per mount: materialize + save_mount/update_mount per conflict_mode.

    Mounts are sorted by mount_point depth (shallowest first) so parent paths
    restore before any nested children that may depend on them.

    Returns per-mount errors; does not raise except on programmer bugs.
    """
    errors: list[ImportError] = []
    overrides = overrides or {}
    sorted_mounts = sorted(mounts, key=lambda m: len(m.mount_point.split("/")))

    for record in sorted_mounts:
        mount_overrides = overrides.get(record.mount_id, {}) or {}
        backend_config = materialize(record, mount_overrides)
        zone_id = target_zone_id if target_zone_id is not None else record.zone_id

        existing = mount_manager.get_mount(record.mount_point)
        if existing is not None:
            if conflict_mode == ConflictMode.SKIP:
                errors.append(
                    ImportError(
                        path=record.mount_point,
                        error_type="conflict",
                        message=f"mount {record.mount_point!r} already exists; skipped",
                    )
                )
                continue
            if conflict_mode == ConflictMode.FAIL:
                errors.append(
                    ImportError(
                        path=record.mount_point,
                        error_type="conflict",
                        message=f"mount {record.mount_point!r} already exists",
                    )
                )
                continue
            if conflict_mode == ConflictMode.OVERWRITE:
                # update_mount only writes backend_config + description.
                # Refuse the overwrite if the existing mount's identity
                # (backend_type, owner) doesn't match — silently writing
                # an S3 backend_config onto a path_local mount, or
                # rebinding ownership to a different user, would corrupt
                # the live mount table on next restore. Reviewer flagged
                # this as a real risk; conflict resolution must not
                # silently change immutable fields.
                existing_backend_type = existing.get("backend_type")
                existing_owner = existing.get("owner_user_id")
                if existing_backend_type and existing_backend_type != record.backend_type:
                    errors.append(
                        ImportError(
                            path=record.mount_point,
                            error_type="conflict",
                            message=(
                                f"mount {record.mount_point!r} backend_type mismatch: "
                                f"existing={existing_backend_type!r} bundle={record.backend_type!r}; "
                                "OVERWRITE refused — remove the mount first or use a different target_zone"
                            ),
                        )
                    )
                    continue
                if (
                    record.owner_user_id is not None
                    and existing_owner is not None
                    and existing_owner != record.owner_user_id
                ):
                    errors.append(
                        ImportError(
                            path=record.mount_point,
                            error_type="conflict",
                            message=(
                                f"mount {record.mount_point!r} owner mismatch: "
                                f"existing={existing_owner!r} bundle={record.owner_user_id!r}; "
                                "OVERWRITE refused — remove the mount first to change ownership"
                            ),
                        )
                    )
                    continue
                mount_manager.update_mount(
                    mount_point=record.mount_point,
                    backend_config=backend_config,
                    description=record.description,
                )
                continue

        try:
            mount_manager.save_mount(
                mount_point=record.mount_point,
                backend_type=record.backend_type,
                backend_config=backend_config,
                owner_user_id=record.owner_user_id,
                zone_id=zone_id,
                description=record.description,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(
                ImportError(
                    path=record.mount_point,
                    error_type="io",
                    message=f"failed to restore mount {record.mount_point!r}: {exc}",
                )
            )

    return errors


__all__ = [
    "read_mounts",
    "validate_overrides",
    "materialize",
    "import_mounts",
]
