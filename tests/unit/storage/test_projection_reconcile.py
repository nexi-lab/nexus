"""storage.projection_reconcile — repair file_paths / version_history from the kernel (#4738).

The kernel listing is a fake ``sys_readdir(details=True)``; the RecordStore
is a real SQLite file.  Rows the observer would have written are seeded
through ``VersionRecorder`` so the tests exercise the same schema the
observer commits.
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from nexus.contracts.metadata import DT_DIR, DT_REG, FileMetadata
from nexus.storage.models import FilePathModel, OperationLogModel, VersionHistoryModel
from nexus.storage.projection_reconcile import (
    RECONCILE_ACTOR,
    list_kernel_files,
    reconcile_projection,
)
from nexus.storage.record_store import SQLAlchemyRecordStore
from nexus.storage.version_recorder import VersionRecorder, version_gen


@pytest.fixture
def record_store() -> Generator[SQLAlchemyRecordStore, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        rs = SQLAlchemyRecordStore(db_path=Path(tmpdir) / "metadata.db")
        yield rs
        rs.close()


class _FakeFS:
    """``sys_readdir(prefix, recursive=True, details=True)`` over a dict of entries."""

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self.entries = entries
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def sys_readdir(self, path: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append((path, kwargs))
        return [
            e for e in self.entries if e["path"].startswith(path.rstrip("/") + "/") or path == "/"
        ]


def _entry(
    path: str, content_id: str, *, gen: int = 1, version: int = 1, **extra: Any
) -> dict[str, Any]:
    base = {
        "path": path,
        "size": 5,
        "content_id": content_id,
        "mime_type": "text/plain",
        "created_at": "2026-09-03T00:00:00+00:00",
        "modified_at": "2026-09-03T00:00:01+00:00",
        "version": version,
        "zone_id": "root",
        "owner_id": "alice",
        "entry_type": DT_REG,
        "gen": gen,
    }
    base.update(extra)
    return base


def _seed(
    record_store: SQLAlchemyRecordStore,
    path: str,
    content_id: str,
    *,
    gen: int,
    is_new: bool = True,
) -> None:
    with record_store.session_factory() as session:
        VersionRecorder(session).record_write(
            FileMetadata(path=path, size=5, content_id=content_id, zone_id="eng", gen=gen),
            is_new=is_new,
        )
        session.commit()


def _versions(record_store: SQLAlchemyRecordStore, path: str) -> list[VersionHistoryModel]:
    with record_store.session_factory() as session:
        fp = session.execute(
            select(FilePathModel).where(
                FilePathModel.virtual_path == path, FilePathModel.deleted_at.is_(None)
            )
        ).scalar_one_or_none()
        if fp is None:
            return []
        return list(
            session.execute(
                select(VersionHistoryModel)
                .where(VersionHistoryModel.resource_id == fp.path_id)
                .order_by(VersionHistoryModel.version_number)
            ).scalars()
        )


def test_list_kernel_files_filters_dirs_system_paths_and_empty_content() -> None:
    fs = _FakeFS(
        [
            _entry("/ws/a.txt", "ca"),
            _entry("/ws/dir", "", entry_type=DT_DIR, is_directory=True),
            _entry("/__sys__/audit/traces/", "x"),
            _entry("/nexus/pipes/audit", "x"),
            _entry("/ws/empty", None),
        ]
    )
    assert [e["path"] for e in list_kernel_files(fs, "/ws")] == ["/ws/a.txt"]
    assert fs.calls[0][1] == {"recursive": True, "details": True, "context": None}


def test_missing_and_drifted_rows_are_created_and_repaired(
    record_store: SQLAlchemyRecordStore,
) -> None:
    # /ws/in-sync: projection matches kernel.  /ws/lost: kernel has a newer
    # content (the write whose projection commit the crash ate).
    # /ws/never: kernel has it, projection never saw it.
    _seed(record_store, "/ws/in-sync", "c-same", gen=3)
    _seed(record_store, "/ws/lost", "c-old", gen=1)
    fs = _FakeFS(
        [
            _entry("/ws/in-sync", "c-same", gen=3),
            _entry("/ws/lost", "c-new", gen=2, version=2),
            _entry("/ws/never", "c-never", gen=1),
        ]
    )

    report = reconcile_projection(
        nexus_fs=fs, session_factory=record_store.session_factory, prefix="/ws", zone_id="eng"
    )

    assert (report.scanned, report.in_sync, report.created, report.repaired) == (3, 1, 1, 1)
    assert report.errors == 0 and report.stale_kernel == 0 and report.retired == 0
    assert report.created_paths == ["/ws/never"] and report.repaired_paths == ["/ws/lost"]

    lost = _versions(record_store, "/ws/lost")
    assert [v.content_id for v in lost] == ["c-old", "c-new"]
    assert [version_gen(v) for v in lost] == [1, 2]
    assert lost[-1].created_by == RECONCILE_ACTOR
    never = _versions(record_store, "/ws/never")
    assert [v.content_id for v in never] == ["c-never"]

    with record_store.session_factory() as session:
        ops = (
            session.execute(select(OperationLogModel).order_by(OperationLogModel.sequence_number))
            .scalars()
            .all()
        )
        assert [(o.path, o.agent_id, o.change_type) for o in ops] == [
            ("/ws/lost", RECONCILE_ACTOR, "upsert"),
            ("/ws/never", RECONCILE_ACTOR, "upsert"),
        ]
        assert all(o.zone_id == "eng" for o in ops)

    # Second pass is a no-op: everything in sync.
    again = reconcile_projection(
        nexus_fs=fs, session_factory=record_store.session_factory, prefix="/ws", zone_id="eng"
    )
    assert (again.in_sync, again.created, again.repaired) == (3, 0, 0)


def test_stale_kernel_view_is_not_written_back(record_store: SQLAlchemyRecordStore) -> None:
    """A lagging follower's older content must not become a 'new' version."""
    _seed(record_store, "/ws/f", "c-v1", gen=1)
    _seed(record_store, "/ws/f", "c-v2", gen=2, is_new=False)
    fs = _FakeFS([_entry("/ws/f", "c-v1", gen=1)])

    report = reconcile_projection(
        nexus_fs=fs, session_factory=record_store.session_factory, prefix="/ws", zone_id="eng"
    )

    assert report.stale_kernel == 1 and report.repaired == 0
    assert report.stale_kernel_paths == ["/ws/f"]
    assert [v.content_id for v in _versions(record_store, "/ws/f")] == ["c-v1", "c-v2"]


def test_path_addressed_backend_same_content_id_is_repaired_by_gen(
    record_store: SQLAlchemyRecordStore,
) -> None:
    """A path-addressed backend reports the path as content_id for every write."""
    _seed(record_store, "/ws/p.txt", "ws/p.txt", gen=1)
    fs = _FakeFS([_entry("/ws/p.txt", "ws/p.txt", gen=2, version=2)])

    report = reconcile_projection(
        nexus_fs=fs, session_factory=record_store.session_factory, prefix="/ws", zone_id="eng"
    )
    assert report.repaired == 1 and report.in_sync == 0
    versions = _versions(record_store, "/ws/p.txt")
    assert [version_gen(v) for v in versions] == [1, 2]
    assert [v.version_number for v in versions] == [1, 2]


def test_equal_gen_is_in_sync_even_when_content_id_differs(
    record_store: SQLAlchemyRecordStore,
) -> None:
    _seed(record_store, "/ws/g.txt", "c-a", gen=4)
    fs = _FakeFS([_entry("/ws/g.txt", "c-b", gen=4)])
    report = reconcile_projection(
        nexus_fs=fs, session_factory=record_store.session_factory, prefix="/ws", zone_id="eng"
    )
    assert report.in_sync == 1 and report.repaired == 0
    assert [v.content_id for v in _versions(record_store, "/ws/g.txt")] == ["c-a"]


def test_legacy_rows_without_gen_fall_back_to_content_comparison(
    record_store: SQLAlchemyRecordStore,
) -> None:
    _seed(record_store, "/ws/legacy", "c-old", gen=0)  # gen 0 → no extra_metadata
    assert version_gen(_versions(record_store, "/ws/legacy")[0]) is None
    fs = _FakeFS([_entry("/ws/legacy", "c-new", gen=5)])

    report = reconcile_projection(
        nexus_fs=fs, session_factory=record_store.session_factory, prefix="/ws", zone_id="eng"
    )
    assert report.repaired == 1
    assert [v.content_id for v in _versions(record_store, "/ws/legacy")] == ["c-old", "c-new"]


def test_rows_projected_under_another_zone_are_matched_not_duplicated(
    record_store: SQLAlchemyRecordStore,
) -> None:
    """The observer keys rows by the writer's zone; a root reconcile must not
    create root-zone duplicates for paths a 'research' token wrote (seen live
    with `nexus demo init`: 6 'created' for rows that existed under zone
    research). Matching is zone-agnostic; repairs stay in the row's zone."""
    with record_store.session_factory() as session:
        VersionRecorder(session).record_write(
            FileMetadata(path="/ws/r.md", size=5, content_id="c1", zone_id="research", gen=1),
            is_new=True,
        )
        session.commit()

    fs = _FakeFS([_entry("/ws/r.md", "c1", gen=1)])
    report = reconcile_projection(
        nexus_fs=fs, session_factory=record_store.session_factory, prefix="/ws", zone_id="root"
    )
    assert (report.in_sync, report.created, report.repaired) == (1, 0, 0)

    # Kernel moved on: the repair lands on the research-zone row, no root row appears.
    fs = _FakeFS([_entry("/ws/r.md", "c2", gen=2, version=2)])
    report = reconcile_projection(
        nexus_fs=fs, session_factory=record_store.session_factory, prefix="/ws", zone_id="root"
    )
    assert report.repaired == 1 and report.created == 0
    with record_store.session_factory() as session:
        rows = (
            session.execute(
                select(FilePathModel).where(
                    FilePathModel.virtual_path == "/ws/r.md", FilePathModel.deleted_at.is_(None)
                )
            )
            .scalars()
            .all()
        )
    assert [(r.zone_id, r.current_version) for r in rows] == [("research", 2)]
    assert [version_gen(v) for v in _versions(record_store, "/ws/r.md")] == [1, 2]


def test_dry_run_reports_without_writing(record_store: SQLAlchemyRecordStore) -> None:
    fs = _FakeFS([_entry("/ws/never", "c")])
    report = reconcile_projection(
        nexus_fs=fs,
        session_factory=record_store.session_factory,
        prefix="/ws",
        zone_id="eng",
        dry_run=True,
    )
    assert report.dry_run is True and report.created == 1
    assert _versions(record_store, "/ws/never") == []


def test_retire_missing_is_opt_in_and_skipped_when_truncated(
    record_store: SQLAlchemyRecordStore,
) -> None:
    _seed(record_store, "/ws/gone", "c-gone", gen=1)
    _seed(record_store, "/ws/keep", "c-keep", gen=1)
    _seed(record_store, "/other/untouched", "c-o", gen=1)
    fs = _FakeFS([_entry("/ws/keep", "c-keep", gen=1)])
    kwargs: dict[str, Any] = {
        "nexus_fs": fs,
        "session_factory": record_store.session_factory,
        "prefix": "/ws",
        "zone_id": "eng",
    }

    # Default: nothing retired.
    assert reconcile_projection(**kwargs).retired == 0
    assert _versions(record_store, "/ws/gone")

    # Truncated walk: retire is skipped even when requested.
    truncated = reconcile_projection(**kwargs, retire_missing=True, max_entries=0)
    assert truncated.truncated is False or truncated.retired == 0

    # Full walk with retire_missing: /ws/gone soft-deleted, /other untouched.
    report = reconcile_projection(**kwargs, retire_missing=True)
    assert report.retired == 1 and report.retired_paths == ["/ws/gone"]
    assert _versions(record_store, "/ws/gone") == []
    assert _versions(record_store, "/other/untouched")
    with record_store.session_factory() as session:
        delete_rows = (
            session.execute(
                select(OperationLogModel).where(OperationLogModel.operation_type == "delete")
            )
            .scalars()
            .all()
        )
        assert [(o.path, o.agent_id) for o in delete_rows] == [("/ws/gone", RECONCILE_ACTOR)]


def test_max_entries_truncates_and_reports(record_store: SQLAlchemyRecordStore) -> None:
    fs = _FakeFS([_entry(f"/ws/f{i}", f"c{i}") for i in range(5)])
    report = reconcile_projection(
        nexus_fs=fs,
        session_factory=record_store.session_factory,
        prefix="/ws",
        zone_id="eng",
        max_entries=2,
    )
    assert report.truncated is True and report.scanned == 2 and report.created == 2


def test_per_entry_failure_is_counted_and_does_not_abort_the_pass(
    record_store: SQLAlchemyRecordStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nexus.storage.projection_reconcile as mod

    real = mod.reconcile_entry

    def flaky(session: Any, md: FileMetadata, **kw: Any) -> str:
        if md.path == "/ws/bad":
            raise RuntimeError("boom")
        return real(session, md, **kw)

    monkeypatch.setattr(mod, "reconcile_entry", flaky)
    fs = _FakeFS([_entry("/ws/bad", "cb"), _entry("/ws/good", "cg")])
    report = reconcile_projection(
        nexus_fs=fs, session_factory=record_store.session_factory, prefix="/ws", zone_id="eng"
    )
    assert report.errors == 1 and report.created == 1
    assert report.error_messages == ["/ws/bad: boom"]
    assert _versions(record_store, "/ws/good")
