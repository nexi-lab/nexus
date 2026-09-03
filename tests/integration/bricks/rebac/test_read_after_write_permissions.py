"""Read-after-write and revocation bounds for a non-admin key (Issue #4739).

Acceptance from the issue:

- A non-admin key writes, then reads, lists and searches immediately and sees
  the file — without waiting for the deferred permission buffer to flush.
- Revoke a viewer: ``check`` denies, ``list`` and ``search`` exclude the file
  immediately (well inside 1 s).

The deferred buffer is configured with a one-hour flush interval so nothing
in these tests can be explained by a background flush.  Tiger is not active
on SQLite; its bypass/invalidation is covered by the unit suites.

``list`` / ``glob`` / ``grep`` go through the search service, which is where
ReBAC filtering for listings lives (``PermissionEnforcer.filter_list``); the
raw ``sys_readdir`` syscall applies zone filtering only and is not asserted on.

Requires the kernel subprocess; set ``NEXUS_BOOTSTRAP_MODE=static`` when the
local ``nexusd-cluster`` build demands a bootstrap mode.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pyroaring")

from nexus import CASLocalBackend
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.types import OperationContext
from nexus.core.config import ParseConfig, PermissionConfig
from nexus.factory import create_nexus_fs
from nexus.storage.record_store import SQLAlchemyRecordStore

TEAM_DIR = "/workspace/team"
NOTES = f"{TEAM_DIR}/notes.md"

ALICE = OperationContext(user_id="alice", groups=[], zone_id=ROOT_ZONE_ID, is_admin=False)
BOB = OperationContext(user_id="bob", groups=[], zone_id=ROOT_ZONE_ID, is_admin=False)
BOB_STRONG = OperationContext(
    user_id="bob", groups=[], zone_id=ROOT_ZONE_ID, is_admin=False, consistency="strong"
)


def _make_nexus(tmp_path: Path, *, sync_owner_grant: bool) -> Any:
    return create_nexus_fs(
        backend=CASLocalBackend(tmp_path / "data"),
        metadata_store=str(tmp_path / "metastore.redb"),
        record_store=SQLAlchemyRecordStore(db_path=tmp_path / "metadata.db"),
        parsing=ParseConfig(auto_parse=False),
        permissions=PermissionConfig(
            enforce=True,
            enable_deferred=True,
            deferred_flush_interval=3600.0,  # never flushes during a test
            sync_owner_grant=sync_owner_grant,
        ),
    )


def _buffer(nx: Any) -> Any:
    try:
        return nx.service("deferred_permission_buffer")
    except Exception:
        return None


def _pending_grants(nx: Any) -> int | None:
    """Pending deferred owner grants, or None when the buffer is not exposed."""
    buf = _buffer(nx)
    if buf is None:
        return None
    return int(buf.get_stats().get("pending_grants", 0))


def _paths(results: list[Any]) -> set[str]:
    out: set[str] = set()
    for r in results:
        if isinstance(r, str):
            out.add(r)
        elif isinstance(r, dict):
            p = r.get("path") or r.get("file")
            if p:
                out.add(str(p))
    return out


@pytest.fixture
def nx(tmp_path: Path) -> Iterator[Any]:
    instance = _make_nexus(tmp_path, sync_owner_grant=True)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def nx_legacy(tmp_path: Path) -> Iterator[Any]:
    instance = _make_nexus(tmp_path, sync_owner_grant=False)
    try:
        yield instance
    finally:
        instance.close()


def _grant(nx: Any, ctx: OperationContext, relation: str, path: str) -> Any:
    return nx.service("rebac_manager").rebac_write(
        subject=ctx.get_subject(),
        relation=relation,
        object=("file", path),
        zone_id=ROOT_ZONE_ID,
    )


def _file_read_check(nx: Any, ctx: OperationContext, path: str, **kw: Any) -> bool:
    return bool(
        nx.service("rebac_manager").rebac_check(
            ctx.get_subject(), "read", ("file", path), zone_id=ROOT_ZONE_ID, **kw
        )
    )


@pytest.mark.asyncio
async def test_non_admin_writer_sees_file_immediately(nx: Any) -> None:
    mgr = nx.service("rebac_manager")
    search = nx.service("search")
    _grant(nx, ALICE, "direct_editor", TEAM_DIR)

    nx.write(NOTES, b"hello nexus", context=ALICE)
    started = time.perf_counter()

    # The owner tuple exists now — not after a flush.
    owner_tuples = mgr.rebac_list_tuples(subject=ALICE.get_subject(), object=("file", NOTES))
    assert any(t.get("relation") == "direct_owner" for t in owner_tuples), owner_tuples
    pending = _pending_grants(nx)
    assert pending in (None, 0), f"owner grant was queued instead of written: {pending}"
    assert _file_read_check(nx, ALICE, NOTES)

    # read / list / glob / grep as the writer, immediately.
    assert nx.sys_read(NOTES, context=ALICE) == b"hello nexus"
    assert NOTES in _paths(search.list(TEAM_DIR, context=ALICE))
    assert NOTES in _paths(search.glob("*.md", TEAM_DIR, context=ALICE))
    assert NOTES in _paths(await search.grep("hello", TEAM_DIR, context=ALICE))

    elapsed = time.perf_counter() - started
    assert elapsed < 5.0, f"read-after-write checks took {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_owner_grant_does_not_depend_on_ancestor_grants(nx: Any) -> None:
    """The file-level tuple alone must make the writer's file visible.

    Alice's editor grant on the directory is revoked after the write, so any
    remaining visibility comes from the synchronously written owner tuple.
    """
    search = nx.service("search")
    editor_grant = _grant(nx, ALICE, "direct_editor", TEAM_DIR)
    nx.write(NOTES, b"hello nexus", context=ALICE)

    assert nx.service("rebac_manager").rebac_delete(editor_grant) is True

    assert _file_read_check(nx, ALICE, NOTES)
    assert nx.sys_read(NOTES, context=ALICE) == b"hello nexus"
    assert NOTES in _paths(search.list(TEAM_DIR, context=ALICE))
    assert NOTES in _paths(search.glob("*.md", TEAM_DIR, context=ALICE))


@pytest.mark.asyncio
async def test_revoked_viewer_is_excluded_within_one_second(nx: Any) -> None:
    mgr = nx.service("rebac_manager")
    search = nx.service("search")
    _grant(nx, ALICE, "direct_editor", TEAM_DIR)
    nx.write(NOTES, b"hello nexus", context=ALICE)

    grant = _grant(nx, BOB, "direct_viewer", NOTES)
    # Warm every cache a viewer can populate.
    assert _file_read_check(nx, BOB, NOTES)
    assert nx.sys_read(NOTES, context=BOB) == b"hello nexus"
    assert NOTES in _paths(search.list(TEAM_DIR, context=BOB))
    assert NOTES in _paths(search.glob("*.md", TEAM_DIR, context=BOB))

    assert mgr.rebac_delete(grant) is True
    revoked_at = time.perf_counter()

    assert not _file_read_check(nx, BOB, NOTES)
    assert NOTES not in _paths(search.list(TEAM_DIR, context=BOB))
    assert NOTES not in _paths(search.glob("*.md", TEAM_DIR, context=BOB))
    assert NOTES not in _paths(await search.grep("hello", TEAM_DIR, context=BOB))
    with pytest.raises(PermissionError):
        nx.sys_read(NOTES, context=BOB)

    elapsed = time.perf_counter() - revoked_at
    assert elapsed < 1.0, f"revocation took {elapsed:.3f}s to be observed"


@pytest.mark.asyncio
async def test_strong_consistency_context_is_honoured_end_to_end(nx: Any) -> None:
    """A strong context resolves from tuples even when the L1 holds a stale allow."""
    mgr = nx.service("rebac_manager")
    search = nx.service("search")
    _grant(nx, ALICE, "direct_editor", TEAM_DIR)
    nx.write(NOTES, b"hello nexus", context=ALICE)

    # Poison the bulk-check L1 with a stale grant for bob on the file.
    mgr._l1_cache.set("user", "bob", "read", "file", NOTES, True, ROOT_ZONE_ID)
    eventual = search.list(TEAM_DIR, context=BOB)
    strong = search.list(TEAM_DIR, context=BOB_STRONG)

    assert NOTES in _paths(eventual)  # cached allow served
    assert NOTES not in _paths(strong)  # tuple store consulted
    # The strong pass repaired the cache for later eventual calls.
    assert NOTES not in _paths(search.list(TEAM_DIR, context=BOB))

    # And for the single-object check API.
    mgr._l1_cache.set("user", "bob", "read", "file", NOTES, True, ROOT_ZONE_ID)
    assert not _file_read_check(nx, BOB, NOTES, consistency="strong")


def test_legacy_deferred_owner_grant_window(nx_legacy: Any) -> None:
    """Documents the pre-#4739 window that ``sync_owner_grant=False`` restores."""
    _grant(nx_legacy, ALICE, "direct_editor", TEAM_DIR)

    nx_legacy.write(NOTES, b"hello nexus", context=ALICE)

    pending = _pending_grants(nx_legacy)
    if pending is None:
        pytest.skip("deferred permission buffer not exposed as a service")
    assert pending == 1
    owner_tuples = nx_legacy.service("rebac_manager").rebac_list_tuples(
        subject=ALICE.get_subject(), object=("file", NOTES)
    )
    assert not any(t.get("relation") == "direct_owner" for t in owner_tuples)

    _buffer(nx_legacy).flush()

    assert _pending_grants(nx_legacy) == 0
    owner_tuples = nx_legacy.service("rebac_manager").rebac_list_tuples(
        subject=ALICE.get_subject(), object=("file", NOTES)
    )
    assert any(t.get("relation") == "direct_owner" for t in owner_tuples)


def _readdir_paths(nx: Any, ctx: OperationContext, **kw: Any) -> set[str]:
    result = nx.sys_readdir(TEAM_DIR, context=ctx, **kw)
    items = getattr(result, "items", result)
    return _paths(list(items))


def test_sys_readdir_is_rebac_filtered_for_non_admin(nx: Any) -> None:
    """The raw readdir syscall (behind ``/v2/files/list``) honours ReBAC.

    Before #4739 ``sys_readdir`` applied zone filtering only, so a key with no
    grant could enumerate names of files it cannot read.
    """
    mgr = nx.service("rebac_manager")
    _grant(nx, ALICE, "direct_editor", TEAM_DIR)
    nx.write(NOTES, b"hello nexus", context=ALICE)
    admin = OperationContext(user_id="admin", groups=[], zone_id=ROOT_ZONE_ID, is_admin=True)

    # bob has no grant: hidden in every listing shape.
    assert NOTES not in _readdir_paths(nx, BOB)
    assert NOTES not in _readdir_paths(nx, BOB, recursive=False)
    assert NOTES not in _readdir_paths(nx, BOB, recursive=False, details=True)
    assert NOTES not in _readdir_paths(nx, BOB, recursive=False, details=True, limit=10)
    # The writer (owner tuple) and admin / system callers still see it.
    assert NOTES in _readdir_paths(nx, ALICE)
    assert NOTES in _readdir_paths(nx, ALICE, recursive=False, details=True)
    assert NOTES in _readdir_paths(nx, admin)

    grant = _grant(nx, BOB, "direct_viewer", NOTES)
    assert NOTES in _readdir_paths(nx, BOB)
    assert NOTES in _readdir_paths(nx, BOB, recursive=False, details=True)

    assert mgr.rebac_delete(grant) is True
    assert NOTES not in _readdir_paths(nx, BOB)
    assert NOTES not in _readdir_paths(nx, BOB, recursive=False, details=True)
