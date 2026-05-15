from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.services.workspace.workspace_manager import WorkspaceManager


class _Session:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, stmt):
        return SimpleNamespace(scalar=lambda: 0)

    def add(self, obj):
        obj.snapshot_id = "snap-1"
        obj.snapshot_number = 1
        obj.manifest_hash = "manifest"
        obj.file_count = 0
        obj.total_size_bytes = 0
        obj.description = None
        obj.created_by = None
        obj.tags = None
        obj.created_at = None

    def commit(self):
        pass

    def refresh(self, obj):
        pass


class _RecordStore:
    def session_factory(self):
        return _Session()


class _Backend:
    def write_content(self, content, context=None):
        return SimpleNamespace(content_id="manifest")


def test_create_snapshot_flushes_workspace_prefix_before_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Kernel:
        def flush_write_buffer(self, path, zone_id):
            calls.append(f"flush:{path}:{zone_id}")
            return SimpleNamespace(flushed=1, failed=0, errors=[])

    monkeypatch.setattr(
        "nexus.kernel_helpers.metastore_list_iter",
        lambda kernel, prefix: calls.append(f"list:{prefix}") or [],
    )

    manager = WorkspaceManager(
        metadata=Kernel(),
        backend=_Backend(),
        rebac_manager=None,
        zone_id="root",
        record_store=_RecordStore(),
    )

    manager.create_snapshot("/workspace")

    assert calls[:2] == ["flush:/workspace:root", "list:/workspace/"]


def test_create_snapshot_fails_when_flush_fails() -> None:
    class Kernel:
        def flush_write_buffer(self, path, zone_id):
            raise RuntimeError("flush failed")

    manager = WorkspaceManager(
        metadata=Kernel(),
        backend=_Backend(),
        rebac_manager=None,
        zone_id="root",
        record_store=_RecordStore(),
    )

    with pytest.raises(RuntimeError, match="flush failed"):
        manager.create_snapshot("/workspace")
