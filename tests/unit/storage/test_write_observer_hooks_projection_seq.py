"""SyncAuditWriteInterceptor relays the observer's projection sequence (#4738)."""

from __future__ import annotations

from typing import Any

from nexus.contracts.metadata import FileMetadata
from nexus.contracts.vfs_hooks import (
    DeleteHookContext,
    RenameHookContext,
    WriteBatchHookContext,
    WriteHookContext,
)
from nexus.storage.write_observer_hooks import SyncAuditWriteInterceptor


class _SeqObserver:
    def __init__(self, seq: Any = 7) -> None:
        self.seq = seq
        self.calls: list[str] = []

    def on_write(self, *_a: Any, **_k: Any) -> Any:
        self.calls.append("write")
        return self.seq

    def on_delete(self, **_k: Any) -> Any:
        self.calls.append("delete")
        return self.seq

    def on_rename(self, **_k: Any) -> Any:
        self.calls.append("rename")
        return self.seq

    def on_write_batch(self, items: list[tuple[Any, bool]], **_k: Any) -> Any:
        self.calls.append("write_batch")
        return [100 + i for i, _ in enumerate(items)]


def test_write_seq_is_stashed_on_ctx_extra() -> None:
    hook = SyncAuditWriteInterceptor(_SeqObserver(seq=7))
    ctx = WriteHookContext(path="/a", content=b"x", context=None)
    hook.on_post_write(ctx)
    assert ctx.extra["projection_seq"] == 7


def test_none_seq_from_legacy_observer_leaves_extra_alone() -> None:
    hook = SyncAuditWriteInterceptor(_SeqObserver(seq=None))
    ctx = WriteHookContext(path="/a", content=b"x", context=None)
    hook.on_post_write(ctx)
    assert "projection_seq" not in ctx.extra


def test_internal_pipe_paths_are_skipped() -> None:
    obs = _SeqObserver()
    hook = SyncAuditWriteInterceptor(obs)
    ctx = WriteHookContext(path="/nexus/pipes/audit", content=b"x", context=None)
    hook.on_post_write(ctx)
    assert obs.calls == [] and ctx.extra == {}


def test_delete_and_rename_seqs_are_stashed() -> None:
    hook = SyncAuditWriteInterceptor(_SeqObserver(seq=9))
    d = DeleteHookContext(path="/a", context=None)
    hook.on_post_delete(d)
    r = RenameHookContext(old_path="/a", new_path="/b", context=None)
    hook.on_post_rename(r)
    assert d.extra["projection_seq"] == 9 and r.extra["projection_seq"] == 9


def test_batch_seqs_map_back_to_every_item_including_filtered_pipes() -> None:
    hook = SyncAuditWriteInterceptor(_SeqObserver())
    items = [
        (FileMetadata(path="/a", size=1, content_id="ca"), True),
        (FileMetadata(path="/nexus/pipes/p", size=1, content_id="cp"), True),
        (FileMetadata(path="/b", size=1, content_id="cb"), False),
    ]
    ctx = WriteBatchHookContext(items=items, context=None, zone_id="z")
    hook.on_post_write_batch(ctx)
    # Pipe item is filtered before the observer sees the batch: it gets None
    # and the others keep their observer-assigned sequences in order.
    assert ctx.extra["projection_seqs"] == [100, None, 101]
