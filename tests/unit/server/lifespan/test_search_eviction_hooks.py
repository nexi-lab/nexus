"""VFS delete / rename / rmdir events must evict what the caller indexed.

Documents are indexed under the caller's token zone with the path the caller
sees (``index_zone_for``).  The eviction hook used to send every delete to the
root zone with the VFS path — ``/zone/<z>/…`` for a zone-scoped caller — so it
matched nothing; directory deletes and renames were never relayed at all
(the kernel removes children without per-child events); and a failed
eviction vanished as an unretrieved task exception.  Each left deleted
documents in search results.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, cast

import pytest

from nexus.contracts.vfs_hooks import DeleteHookContext, RenameHookContext, RmdirHookContext
from nexus.server.lifespan.search import _wire_notify_hooks, search_zone_and_path


class _FakeFS:
    def __init__(self) -> None:
        self.hooks: dict[str, Any] = {}

    def register_intercept_delete(self, hook: Any) -> None:
        self.hooks["delete"] = hook

    def register_intercept_rename(self, hook: Any) -> None:
        self.hooks["rename"] = hook

    def register_intercept_rmdir(self, hook: Any) -> None:
        self.hooks["rmdir"] = hook


class _FakeDaemon:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.fail = fail

    async def notify_file_change(
        self, path: str, change_type: str = "update", *, zone_id: str | None = None
    ) -> dict[str, Any]:
        self.calls.append((path, change_type, zone_id))
        if self.fail:
            raise RuntimeError("plugin notify_file_change failed: boom")
        return {"status": "accepted", "index_seq": 1}


async def _wire(daemon: _FakeDaemon) -> _FakeFS:
    fs = _FakeFS()
    app = SimpleNamespace(state=SimpleNamespace(search_daemon=daemon))
    _wire_notify_hooks(cast(Any, app), cast(Any, SimpleNamespace(nexus_fs=fs)))
    return fs


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def test_zone_and_path_mapping() -> None:
    assert search_zone_and_path("/workspaces/a.txt", None) == ("root", "/workspaces/a.txt")
    assert search_zone_and_path("/workspaces/a.txt", "root") == ("root", "/workspaces/a.txt")
    assert search_zone_and_path("/zone/z1/workspaces/a.txt", "z1") == ("z1", "/workspaces/a.txt")
    # Only the caller's own zone prefix is stripped (no /zone/z10 for z1).
    assert search_zone_and_path("/zone/z10/a.txt", "z1") == ("z1", "/zone/z10/a.txt")
    assert search_zone_and_path("/zone/z1", "z1") == ("z1", "/")


@pytest.mark.asyncio
async def test_events_evict_in_the_callers_zone_and_cover_subtrees() -> None:
    daemon = _FakeDaemon()
    fs = await _wire(daemon)

    def fire() -> None:  # VFS hooks run on worker threads
        fs.hooks["delete"].on_post_delete(
            DeleteHookContext(path="/zone/z1/workspaces/w/a.txt", context=None, zone_id="z1")
        )
        fs.hooks["delete"].on_post_delete(
            DeleteHookContext(path="/workspaces/w/b.txt", context=None)
        )
        fs.hooks["rmdir"].on_post_rmdir(
            RmdirHookContext(path="/zone/z1/workspaces/w/notes", context=None, zone_id="z1")
        )
        fs.hooks["rename"].on_post_rename(
            RenameHookContext(
                old_path="/workspaces/w/old-dir",
                new_path="/workspaces/w/new-dir",
                context=None,
                is_directory=True,
            )
        )
        fs.hooks["rename"].on_post_rename(
            RenameHookContext(old_path="/workspaces/w/c.txt", new_path="/x.txt", context=None)
        )

    await asyncio.to_thread(fire)
    await _settle()
    assert daemon.calls == [
        ("/workspaces/w/a.txt", "delete", "z1"),
        ("/workspaces/w/b.txt", "delete", "root"),
        ("/workspaces/w/notes", "delete_prefix", "z1"),
        ("/workspaces/w/old-dir", "delete_prefix", "root"),
        ("/workspaces/w/c.txt", "delete", "root"),
    ]


@pytest.mark.asyncio
async def test_failed_eviction_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    daemon = _FakeDaemon(fail=True)
    fs = await _wire(daemon)
    with caplog.at_level(logging.WARNING, logger="nexus.server.lifespan.search"):
        await asyncio.to_thread(
            fs.hooks["delete"].on_post_delete,
            DeleteHookContext(path="/workspaces/w/a.txt", context=None),
        )
        await _settle()
    assert daemon.calls == [("/workspaces/w/a.txt", "delete", "root")]
    assert any("search eviction failed" in r.getMessage() for r in caplog.records)
