"""Repair the RecordStore projection from the kernel (#4738).

The write observer commits ``file_paths`` / ``version_history`` /
``operation_log`` rows before a mutation returns, but the kernel commit
happens first: a ``kill -9`` between the two, a RecordStore outage that
outlived the observer's retry budget, or a dropped queue entry leaves the
kernel (authoritative, durable) ahead of the projection.  This module
walks the kernel under a prefix and brings the projection back in line:

* path in the kernel, no active ``file_paths`` row → ``created`` (a
  ``file_paths`` row + ``version_history`` v1 from the kernel's current
  metadata, plus an ``operation_log`` write row attributed to
  ``system:projection-reconcile``);
* row present and the head ``version_history`` row records a kernel
  ``gen``: kernel ``gen`` newer → ``repaired`` (a new version row for the
  kernel's current content); equal → ``in_sync``; older → ``stale_kernel``
  (this node's kernel view is the lagging one — a follower — and the path
  is left alone);
* row present, legacy head row without a ``gen``: fall back to comparing
  ``content_id`` (differs → ``repaired``, same → ``in_sync``);
* with ``retire_missing=True``, active rows under the prefix the kernel no
  longer has → ``retired`` (soft delete).

``gen`` is the primary key of the comparison because a path-addressed
backend (the local backend in a single-node deployment) reports the
storage key — the path — as ``content_id``, which never changes across
writes; ``gen`` increments on every write everywhere.

Intermediate versions lost in the crash window cannot be recovered — the
kernel keeps only the current metadata — so the repair records the
current content as the next version.  Exposed as
``POST /api/v2/admin/reconcile-projections``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from sqlalchemy import select

from nexus.contracts.constants import ROOT_ZONE_ID, SYSTEM_PATH_PREFIX
from nexus.contracts.metadata import DT_REG, FileMetadata
from nexus.storage.models import FilePathModel, VersionHistoryModel
from nexus.storage.version_recorder import VersionRecorder, version_gen

logger = logging.getLogger(__name__)

RECONCILE_ACTOR = "system:projection-reconcile"
_INTERNAL_PIPE_PREFIX = "/nexus/pipes/"
_SAMPLE_CAP = 25


@dataclass
class ReconcileReport:
    """Outcome of one reconcile pass; every scanned path lands in one bucket."""

    prefix: str
    zone_id: str
    dry_run: bool
    scanned: int = 0
    in_sync: int = 0
    created: int = 0
    repaired: int = 0
    retired: int = 0
    stale_kernel: int = 0
    errors: int = 0
    truncated: bool = False
    duration_ms: int = 0
    created_paths: list[str] = field(default_factory=list)
    repaired_paths: list[str] = field(default_factory=list)
    retired_paths: list[str] = field(default_factory=list)
    stale_kernel_paths: list[str] = field(default_factory=list)
    error_messages: list[str] = field(default_factory=list)

    def _note(self, bucket: list[str], value: str) -> None:
        if len(bucket) < _SAMPLE_CAP:
            bucket.append(value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "prefix": self.prefix,
            "zone_id": self.zone_id,
            "dry_run": self.dry_run,
            "scanned": self.scanned,
            "in_sync": self.in_sync,
            "created": self.created,
            "repaired": self.repaired,
            "retired": self.retired,
            "stale_kernel": self.stale_kernel,
            "errors": self.errors,
            "truncated": self.truncated,
            "duration_ms": self.duration_ms,
            "created_paths": list(self.created_paths),
            "repaired_paths": list(self.repaired_paths),
            "retired_paths": list(self.retired_paths),
            "stale_kernel_paths": list(self.stale_kernel_paths),
            "error_messages": list(self.error_messages),
        }


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _is_projected_file(entry: Any) -> bool:
    """Regular files with content, outside the kernel-internal namespaces."""
    if not isinstance(entry, dict):
        return False
    path = entry.get("path")
    if not isinstance(path, str) or not path.startswith("/"):
        return False
    if path.startswith(SYSTEM_PATH_PREFIX) or path.startswith(_INTERNAL_PIPE_PREFIX):
        return False
    if entry.get("is_directory"):
        return False
    entry_type = entry.get("entry_type", DT_REG)
    if entry_type not in (DT_REG, None):
        return False
    return bool(entry.get("content_id"))


def _entry_metadata(entry: dict[str, Any], zone_id: str) -> FileMetadata:
    """Kernel listing entry → FileMetadata keyed by the caller's zone.

    The observer keys ``file_paths`` rows by the *writer's* zone (the
    context's ``zone_id``), not the mount's, so the repair uses the token
    zone the admin is reconciling for and ignores ``entry["zone_id"]``.
    """
    return FileMetadata(
        path=entry["path"],
        size=int(entry.get("size") or 0),
        content_id=entry.get("content_id"),
        mime_type=entry.get("mime_type"),
        created_at=_parse_ts(entry.get("created_at")),
        modified_at=_parse_ts(entry.get("modified_at")),
        version=int(entry.get("version") or 1),
        zone_id=zone_id,
        owner_id=entry.get("owner_id"),
        gen=int(entry.get("gen") or 0),
    )


def list_kernel_files(nexus_fs: Any, prefix: str, *, context: Any = None) -> list[dict[str, Any]]:
    """Recursive kernel listing under ``prefix`` filtered to projected files."""
    raw = nexus_fs.sys_readdir(prefix, recursive=True, details=True, context=context)
    entries: list[Any]
    if isinstance(raw, dict):
        entries = list(raw.get("entries") or raw.get("items") or [])
    else:
        entries = list(raw or [])
    return [e for e in entries if _is_projected_file(e)]


def _head_version(session: Any, path_id: str) -> VersionHistoryModel | None:
    head: VersionHistoryModel | None = session.execute(
        select(VersionHistoryModel)
        .where(
            VersionHistoryModel.resource_type == "file",
            VersionHistoryModel.resource_id == path_id,
        )
        .order_by(VersionHistoryModel.version_number.desc())
        .limit(1)
    ).scalar_one_or_none()
    return head


def _log_row(op_logger: Any, md: FileMetadata, *, zone_id: str, operation: str) -> None:
    from nexus.contracts.urn import NexusURN

    op_logger.log_operation(
        operation_type=operation,
        path=md.path,
        zone_id=zone_id,
        agent_id=RECONCILE_ACTOR,
        metadata_snapshot=md.to_dict() if operation == "write" else None,
        status="success",
        entity_urn=str(NexusURN.for_file(zone_id, md.path)),
        aspect_name="file_metadata",
        change_type="upsert" if operation == "write" else "delete",
    )


def reconcile_entry(
    session: Any,
    md: FileMetadata,
    *,
    zone_id: str,
    dry_run: bool = False,
) -> str:
    """Reconcile one kernel entry; returns ``in_sync|created|repaired|stale_kernel``.

    Rows are matched by ``virtual_path`` across zones: the observer keys
    ``file_paths`` by the *writer's* zone (a ``research`` token's writes sit
    under ``research``) while ``list_versions`` looks a path up by
    ``virtual_path`` alone, so a root-zone reconcile must adopt those rows,
    not duplicate them.  ``zone_id`` (the caller's) is used only for rows
    that exist in no zone; repairs stay in the matched row's zone.
    """
    from nexus.storage.operation_logger import OperationLogger

    rows = list(
        session.execute(
            select(FilePathModel)
            .where(
                FilePathModel.virtual_path == md.path,
                FilePathModel.deleted_at.is_(None),
            )
            .order_by(FilePathModel.current_version.desc(), FilePathModel.updated_at.desc())
        ).scalars()
    )
    # Prefer the caller's zone when the path was projected under several.
    row = next((r for r in rows if r.zone_id == zone_id), rows[0] if rows else None)

    if row is None:
        if not dry_run:
            VersionRecorder(session).record_write(md, is_new=True, created_by=RECONCILE_ACTOR)
            _log_row(OperationLogger(session), md, zone_id=zone_id, operation="write")
        return "created"

    if row.zone_id != zone_id:
        # Repair against the zone the projection already uses for this path.
        zone_id = row.zone_id
        md = replace(md, zone_id=zone_id)

    head = _head_version(session, row.path_id)
    head_gen = version_gen(head) if head is not None else None
    if head_gen is not None and md.gen:
        # Both sides know the kernel generation: it, not content_id, decides
        # (path-addressed backends report the same content_id for every write).
        if md.gen == head_gen:
            return "in_sync"
        if md.gen < head_gen:
            # The projection already holds a newer kernel generation: this
            # node's kernel view is behind (follower lag), not the projection.
            return "stale_kernel"
    else:
        head_content_id = head.content_id if head is not None else row.content_id
        if head_content_id == md.content_id:
            return "in_sync"

    if not dry_run:
        VersionRecorder(session).record_write(md, is_new=False, created_by=RECONCILE_ACTOR)
        _log_row(OperationLogger(session), md, zone_id=zone_id, operation="write")
    return "repaired"


def reconcile_projection(
    *,
    nexus_fs: Any,
    session_factory: Any,
    prefix: str = "/",
    zone_id: str | None = None,
    context: Any = None,
    dry_run: bool = False,
    retire_missing: bool = False,
    max_entries: int | None = None,
    batch_size: int = 200,
) -> ReconcileReport:
    """Bring the projection under ``prefix`` in line with the kernel.

    ``retire_missing`` soft-deletes active rows the kernel no longer lists;
    it is opt-in because a partial kernel listing (an unrouted mount) would
    otherwise retire live rows.  It is skipped when the walk was truncated
    by ``max_entries``.
    """
    from nexus.storage.operation_logger import OperationLogger

    started = time.monotonic()
    zone = zone_id or ROOT_ZONE_ID
    report = ReconcileReport(prefix=prefix, zone_id=zone, dry_run=dry_run)

    entries = list_kernel_files(nexus_fs, prefix, context=context)
    if max_entries is not None and len(entries) > max_entries:
        entries = entries[:max_entries]
        report.truncated = True

    kernel_paths: set[str] = set()
    for start in range(0, len(entries), max(1, batch_size)):
        chunk = entries[start : start + max(1, batch_size)]
        with session_factory() as session:
            for entry in chunk:
                report.scanned += 1
                md = _entry_metadata(entry, zone)
                kernel_paths.add(md.path)
                try:
                    with session.begin_nested():
                        verdict = reconcile_entry(session, md, zone_id=zone, dry_run=dry_run)
                except Exception as exc:
                    report.errors += 1
                    report._note(report.error_messages, f"{md.path}: {exc}")
                    logger.warning("projection reconcile failed for %s: %s", md.path, exc)
                    continue
                if verdict == "created":
                    report.created += 1
                    report._note(report.created_paths, md.path)
                elif verdict == "repaired":
                    report.repaired += 1
                    report._note(report.repaired_paths, md.path)
                elif verdict == "stale_kernel":
                    report.stale_kernel += 1
                    report._note(report.stale_kernel_paths, md.path)
                else:
                    report.in_sync += 1
            if dry_run:
                session.rollback()
            else:
                session.commit()

    if retire_missing and not report.truncated:
        like = "%" if prefix in ("", "/") else prefix.rstrip("/") + "/%"
        with session_factory() as session:
            rows = list(
                session.execute(
                    select(FilePathModel).where(
                        FilePathModel.zone_id == zone,
                        FilePathModel.deleted_at.is_(None),
                        FilePathModel.virtual_path.like(like),
                    )
                ).scalars()
            )
            recorder = VersionRecorder(session)
            op_logger = OperationLogger(session)
            for row in rows:
                path = row.virtual_path
                if path in kernel_paths or path.startswith(SYSTEM_PATH_PREFIX):
                    continue
                report.retired += 1
                report._note(report.retired_paths, path)
                if dry_run:
                    continue
                try:
                    with session.begin_nested():
                        recorder.record_delete(path)
                        _log_row(
                            op_logger,
                            FileMetadata(path=path, size=0, zone_id=zone),
                            zone_id=zone,
                            operation="delete",
                        )
                except Exception as exc:
                    report.errors += 1
                    report._note(report.error_messages, f"{path}: {exc}")
            if dry_run:
                session.rollback()
            else:
                session.commit()

    report.duration_ms = int((time.monotonic() - started) * 1000)
    if report.created or report.repaired or report.retired:
        logger.warning(
            "projection reconcile under %s (zone %s%s): scanned=%d created=%d repaired=%d "
            "retired=%d stale_kernel=%d errors=%d",
            prefix,
            zone,
            ", dry run" if dry_run else "",
            report.scanned,
            report.created,
            report.repaired,
            report.retired,
            report.stale_kernel,
            report.errors,
        )
    return report
