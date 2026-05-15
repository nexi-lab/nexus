"""Search mutation event contract and normalization helpers.

The search indexing pipeline consumes filesystem mutations from the durable
operation log. This module converts those records into a small, explicit
event shape so each consumer sees the same semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class SearchMutationOp(StrEnum):
    """Normalized operation type for search index maintenance."""

    UPSERT = "upsert"
    DELETE = "delete"


@dataclass(frozen=True)
class SearchMutationEvent:
    """Normalized filesystem mutation for search consumers."""

    event_id: str
    operation_id: str
    op: SearchMutationOp
    path: str
    zone_id: str
    timestamp: datetime
    sequence_number: int
    new_path: str | None = None

    @property
    def virtual_path(self) -> str:
        """Return the unscoped DB virtual_path for this event."""
        return strip_zone_prefix(self.path)

    @classmethod
    def from_operation_log_row(cls, row: Any) -> "SearchMutationEvent | None":
        """Build a normalized event from an OperationLogModel-like row.

        Returns None for operation types that do not affect search indexing.
        """
        op = infer_mutation_op(
            operation_type=getattr(row, "operation_type", None),
            change_type=getattr(row, "change_type", None),
        )
        if op is None:
            return None

        created_at = getattr(row, "created_at", None)
        if created_at is None:
            created_at = datetime.now(UTC).replace(tzinfo=None)
        elif getattr(created_at, "tzinfo", None) is not None:
            created_at = created_at.astimezone(UTC).replace(tzinfo=None)

        sequence_number = getattr(row, "sequence_number", None)
        if sequence_number is None:
            raise ValueError("operation log row missing sequence_number")

        operation_id = getattr(row, "operation_id", None)
        if operation_id is None:
            raise ValueError("operation log row missing operation_id")

        path = getattr(row, "path", None)
        if not path:
            raise ValueError("operation log row missing path")

        zone_id = getattr(row, "zone_id", None) or "root"

        return cls(
            event_id=f"search:{operation_id}",
            operation_id=operation_id,
            op=op,
            path=path,
            zone_id=zone_id,
            timestamp=created_at,
            sequence_number=int(sequence_number),
            new_path=getattr(row, "new_path", None),
        )


def infer_mutation_op(
    *,
    operation_type: str | None,
    change_type: str | None,
) -> SearchMutationOp | None:
    """Map operation log fields to a normalized search mutation op."""
    if operation_type == "write":
        return SearchMutationOp.UPSERT
    if operation_type == "delete":
        return SearchMutationOp.DELETE
    if operation_type == "rename":
        if change_type == "delete":
            return SearchMutationOp.DELETE
        return SearchMutationOp.UPSERT
    return None


def strip_zone_prefix(path: str) -> str:
    """Strip /zone/{zone_id} prefix from a scoped VFS path."""
    import re

    match = re.match(r"^/zone/[^/]+(/.*)", path)
    return match.group(1) if match else path


def extract_zone_id(path: str, default: str = "root") -> str:
    """Extract zone ID from a scoped path when present."""
    import re

    match = re.match(r"^/zone/([^/]+)/", path)
    return match.group(1) if match else default
