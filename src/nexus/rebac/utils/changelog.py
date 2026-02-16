"""Changelog entry helper for rebac_changelog table.

Replaces 7+ duplicated ``INSERT INTO rebac_changelog`` SQL blocks with a
single reusable constant and helper functions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from nexus.rebac.utils.zone import normalize_zone_id

# Canonical column order — every caller must use this order.
CHANGELOG_INSERT_SQL = """
INSERT INTO rebac_changelog (
    change_type, tuple_id, subject_type, subject_id,
    relation, object_type, object_id, zone_id, created_at
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def changelog_params(
    *,
    change_type: str,
    tuple_id: str,
    subject_type: str,
    subject_id: str,
    relation: str,
    object_type: str,
    object_id: str,
    zone_id: str | None = None,
    created_at: str | None = None,
) -> tuple[str, str, str, str, str, str, str, str, str]:
    """Build a parameter tuple for :data:`CHANGELOG_INSERT_SQL`.

    Parameters are returned in the canonical column order so they can be
    passed directly to ``cursor.execute()`` or accumulated into a list for
    ``cursor.executemany()``.
    """
    return (
        change_type,
        tuple_id,
        subject_type,
        subject_id,
        relation,
        object_type,
        object_id,
        normalize_zone_id(zone_id),
        created_at or datetime.now(UTC).isoformat(),
    )


def insert_changelog_entry(
    cursor: Any,
    fix_sql_fn: Any,
    *,
    change_type: str,
    tuple_id: str,
    subject_type: str,
    subject_id: str,
    relation: str,
    object_type: str,
    object_id: str,
    zone_id: str | None = None,
    created_at: str | None = None,
) -> None:
    """Insert a single changelog row.

    Convenience wrapper around :data:`CHANGELOG_INSERT_SQL` +
    :func:`changelog_params`.  Calls ``cursor.execute(...)`` directly.
    """
    cursor.execute(
        fix_sql_fn(CHANGELOG_INSERT_SQL),
        changelog_params(
            change_type=change_type,
            tuple_id=tuple_id,
            subject_type=subject_type,
            subject_id=subject_id,
            relation=relation,
            object_type=object_type,
            object_id=object_id,
            zone_id=zone_id,
            created_at=created_at,
        ),
    )


def insert_changelog_entries_batch(
    cursor: Any,
    fix_sql_fn: Any,
    entries: list[tuple[str, str, str, str, str, str, str, str, str]],
) -> None:
    """Bulk-insert changelog rows via ``executemany``.

    Each element of *entries* must already be in the canonical column order
    (as returned by :func:`changelog_params`).
    """
    cursor.executemany(fix_sql_fn(CHANGELOG_INSERT_SQL), entries)
