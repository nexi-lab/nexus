"""sys_readdir zone-visibility matrix (#4740) against a fake kernel.

Runs without the kernel binary: ``MetadataMixin.sys_readdir`` is exercised
on a stub NexusFS whose kernel serves a fixed metastore.  Covers the three
fail-open paths the issue names — zone-less contexts resolving to ROOT,
root-tagged entries visible from every zone, admins skipping the zone
predicate — across the recursive (metastore scan), non-recursive fast path
(kernel readdir + stat_batch) and paginated branches.
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import PermissionDeniedError
from nexus.contracts.metadata import FileMetadata
from nexus.contracts.types import OperationContext
from nexus.core.nexus_fs_metadata import MetadataMixin
from nexus.lib.events import register_audit_sink
from nexus.lib.zone_visibility import ALL_ZONES_AUDIT_EVENT

ROOT_FILE = "/root.txt"  # root-tagged, root namespace
FLAT_TA = "/flat-ta.txt"  # tenant row stored flat (standalone mode)
TA_FILE = "/zone/ta/a.txt"
TA_ROOTY = "/zone/ta/rooty.txt"  # root-tagged row written inside tenant A's namespace
TB_FILE = "/zone/tb/b.txt"

ALL_PATHS = {ROOT_FILE, FLAT_TA, TA_FILE, TA_ROOTY, TB_FILE}


def _entries() -> list[FileMetadata]:
    return [
        FileMetadata(path=ROOT_FILE, size=1, zone_id=ROOT_ZONE_ID),
        FileMetadata(path=FLAT_TA, size=1, zone_id="ta"),
        FileMetadata(path=TA_FILE, size=1, zone_id="ta"),
        FileMetadata(path=TA_ROOTY, size=1, zone_id=None),
        FileMetadata(path=TB_FILE, size=1, zone_id="tb"),
    ]


class _FakeKernel:
    def __init__(self, entries: list[FileMetadata]) -> None:
        self.entries = entries
        self.stat_batch_calls = 0

    def metastore_list_paginated(
        self, prefix: str, recursive: bool, limit: int, cursor: Any
    ) -> dict[str, Any]:
        items = [e for e in self.entries if e.path.startswith(prefix)]
        if not recursive:
            items = [e for e in items if "/" not in e.path[len(prefix) :].lstrip("/")]
        if cursor:
            items = [e for e in items if e.path > cursor]
        page = items[:limit]
        return {
            "items": page,
            "next_cursor": page[-1].path if len(items) > limit else None,
            "has_more": len(items) > limit,
            "total_count": len(items),
        }

    def sys_readdir(self, path: str, zone_id: str, is_admin: bool) -> list[tuple[str, int]]:
        base = "" if path == "/" else path.rstrip("/")
        out: list[tuple[str, int]] = []
        for e in self.entries:
            if e.path.startswith(base + "/") and "/" not in e.path[len(base) + 1 :]:
                out.append((e.path, e.entry_type))
        return out

    def stat_batch(self, paths: list[str], zone_id: str) -> list[dict[str, Any] | None]:
        self.stat_batch_calls += 1
        by_path = {e.path: e for e in self.entries}
        return [
            {"path": p, "zone_id": by_path[p].zone_id, "is_directory": False}
            if p in by_path
            else None
            for p in paths
        ]

    def sys_stat(self, path: str, zone_id: str) -> dict[str, Any] | None:
        for e in self.entries:
            if e.path == path:
                return {"path": path, "zone_id": e.zone_id, "is_directory": False}
        return None


class _FakeFS(MetadataMixin):
    """Just enough NexusFS surface for MetadataMixin.sys_readdir."""

    def __init__(self, entries: list[FileMetadata]) -> None:
        self._kernel = _FakeKernel(entries)
        self._zone_id = ROOT_ZONE_ID
        self._hook_specs = {}
        self.metadata = None
        self._driver_coordinator = None

    def _get_context_identity(self, context: Any = None) -> tuple[str | None, str | None, bool]:
        if context is None:
            return (ROOT_ZONE_ID, None, True)
        if isinstance(context, dict):
            return (context.get("zone_id"), context.get("agent_id"), context.get("is_admin", False))
        return (context.zone_id, context.agent_id, getattr(context, "is_admin", False))

    def sys_stat(self, path: str, *, context: Any = None) -> dict[str, Any] | None:
        return self._kernel.sys_stat(path, ROOT_ZONE_ID)


@pytest.fixture
def fs() -> _FakeFS:
    return _FakeFS(_entries())


def _ctx(**kwargs: Any) -> OperationContext:
    kwargs.setdefault("user_id", "alice")
    kwargs.setdefault("groups", [])
    return OperationContext(**kwargs)


def _paths(result: Any) -> set[str]:
    items = result.items if hasattr(result, "items") and not isinstance(result, list) else result
    return {i["path"] if isinstance(i, dict) else i for i in items}


TENANT_A_VISIBLE = {FLAT_TA, TA_FILE, TA_ROOTY}
TENANT_B_VISIBLE = {TB_FILE}
ROOT_VISIBLE = {ROOT_FILE}


class TestRecursiveScan:
    """recursive=True → metastore prefix scan + Python post-filter."""

    def test_kernel_caller_sees_everything(self, fs: _FakeFS) -> None:
        assert _paths(fs.sys_readdir("/", context=None)) == ALL_PATHS

    def test_tenant_a_sees_only_its_zone_including_flat_and_rooty(self, fs: _FakeFS) -> None:
        assert _paths(fs.sys_readdir("/", context=_ctx(zone_id="ta"))) == TENANT_A_VISIBLE

    def test_tenant_b_never_sees_tenant_a_or_root(self, fs: _FakeFS) -> None:
        assert _paths(fs.sys_readdir("/", context=_ctx(user_id="bob", zone_id="tb"))) == (
            TENANT_B_VISIBLE
        )

    def test_root_zone_caller_sees_root_tagged_only(self, fs: _FakeFS) -> None:
        assert _paths(fs.sys_readdir("/", context=_ctx(zone_id=ROOT_ZONE_ID))) == ROOT_VISIBLE

    def test_zone_less_non_admin_is_refused(self, fs: _FakeFS) -> None:
        with pytest.raises(PermissionDeniedError):
            fs.sys_readdir("/", context=_ctx())

    def test_zone_less_dict_context_is_refused(self, fs: _FakeFS) -> None:
        with pytest.raises(PermissionDeniedError):
            fs.sys_readdir("/", context={"user_id": "alice", "is_admin": False})

    def test_admin_without_all_zones_sees_root_zone_only(self, fs: _FakeFS) -> None:
        assert _paths(fs.sys_readdir("/", context=_ctx(is_admin=True))) == ROOT_VISIBLE

    def test_admin_in_tenant_zone_sees_that_zone_only(self, fs: _FakeFS) -> None:
        assert _paths(fs.sys_readdir("/", context=_ctx(is_admin=True, zone_id="ta"))) == (
            TENANT_A_VISIBLE
        )

    def test_admin_all_zones_sees_everything_and_is_audited_once(self, fs: _FakeFS) -> None:
        seen: list[tuple[str, dict]] = []
        handle = register_audit_sink(lambda n, p: seen.append((n, p)))
        try:
            ctx = _ctx(user_id="root-op", is_admin=True)
            assert _paths(fs.sys_readdir("/", context=ctx, all_zones=True)) == ALL_PATHS
        finally:
            handle.remove()
        assert [n for n, _ in seen] == [ALL_ZONES_AUDIT_EVENT]
        assert seen[0][1]["operation"] == "list"
        assert seen[0][1]["subject_id"] == "root-op"

    def test_non_admin_all_zones_is_refused_not_narrowed(self, fs: _FakeFS) -> None:
        with pytest.raises(PermissionDeniedError, match="all_zones"):
            fs.sys_readdir("/", context=_ctx(zone_id="ta"), all_zones=True)

    def test_multi_zone_token_sees_readable_zones_not_root(self, fs: _FakeFS) -> None:
        ctx = _ctx(zone_id=ROOT_ZONE_ID, zone_perms=(("ta", "r"), ("tb", "w")))
        assert _paths(fs.sys_readdir("/", context=ctx)) == TENANT_A_VISIBLE

    def test_system_context_with_zone_keeps_root_namespace_rows(self, fs: _FakeFS) -> None:
        ctx = _ctx(user_id="system", is_system=True, zone_id="ta")
        assert _paths(fs.sys_readdir("/", context=ctx)) == TENANT_A_VISIBLE | ROOT_VISIBLE

    def test_system_context_without_zone_is_unrestricted(self, fs: _FakeFS) -> None:
        ctx = _ctx(user_id="system", is_system=True)
        assert _paths(fs.sys_readdir("/", context=ctx)) == ALL_PATHS

    def test_details_projection_is_filtered_too(self, fs: _FakeFS) -> None:
        rows = fs.sys_readdir("/", details=True, context=_ctx(zone_id="tb"))
        assert {r["path"] for r in rows} == TENANT_B_VISIBLE
        assert all(r["zone_id"] == "tb" for r in rows)


class TestNonRecursiveKernelFastPath:
    """recursive=False, details=False, no limit → kernel readdir + stat_batch."""

    def test_tenant_a_at_root_sees_flat_row_not_root_file(self, fs: _FakeFS) -> None:
        assert _paths(fs.sys_readdir("/", recursive=False, context=_ctx(zone_id="ta"))) == {FLAT_TA}
        assert fs._kernel.stat_batch_calls == 1

    def test_tenant_a_inside_its_namespace(self, fs: _FakeFS) -> None:
        got = fs.sys_readdir("/zone/ta", recursive=False, context=_ctx(zone_id="ta"))
        assert _paths(got) == {TA_FILE, TA_ROOTY}

    def test_root_caller_cannot_enumerate_tenant_namespace(self, fs: _FakeFS) -> None:
        got = fs.sys_readdir("/zone/ta", recursive=False, context=_ctx(zone_id=ROOT_ZONE_ID))
        assert _paths(got) == set()

    def test_root_caller_at_root(self, fs: _FakeFS) -> None:
        got = fs.sys_readdir("/", recursive=False, context=_ctx(zone_id=ROOT_ZONE_ID))
        assert _paths(got) == ROOT_VISIBLE

    def test_unrestricted_callers_skip_stat_batch(self, fs: _FakeFS) -> None:
        got = fs.sys_readdir("/", recursive=False, context=None)
        assert _paths(got) == {ROOT_FILE, FLAT_TA}
        assert fs._kernel.stat_batch_calls == 0

    def test_admin_all_zones_fast_path(self, fs: _FakeFS) -> None:
        got = fs.sys_readdir(
            "/zone/tb", recursive=False, context=_ctx(is_admin=True), all_zones=True
        )
        assert _paths(got) == {TB_FILE}

    def test_zone_less_non_admin_is_refused_before_kernel(self, fs: _FakeFS) -> None:
        with pytest.raises(PermissionDeniedError):
            fs.sys_readdir("/", recursive=False, context=_ctx())


class TestPaginated:
    def test_paginated_recursive_is_filtered(self, fs: _FakeFS) -> None:
        page = fs.sys_readdir("/", recursive=True, limit=10, context=_ctx(zone_id="ta"))
        assert _paths(page) == TENANT_A_VISIBLE
        assert page.has_more is False

    def test_paginated_non_recursive_is_filtered(self, fs: _FakeFS) -> None:
        page = fs.sys_readdir("/", recursive=False, limit=10, context=_ctx(zone_id=ROOT_ZONE_ID))
        assert _paths(page) == ROOT_VISIBLE

    def test_paginated_admin_all_zones_audits_once(self, fs: _FakeFS) -> None:
        seen: list[str] = []
        handle = register_audit_sink(lambda n, _p: seen.append(n))
        try:
            page = fs.sys_readdir(
                "/", recursive=True, limit=10, context=_ctx(is_admin=True), all_zones=True
            )
        finally:
            handle.remove()
        assert _paths(page) == ALL_PATHS
        assert seen == [ALL_ZONES_AUDIT_EVENT]

    def test_paginated_zone_less_non_admin_is_refused(self, fs: _FakeFS) -> None:
        with pytest.raises(PermissionDeniedError):
            fs.sys_readdir("/", recursive=True, limit=10, context=_ctx())
