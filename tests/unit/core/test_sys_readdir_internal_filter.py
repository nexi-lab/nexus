"""Tests for Issue #3388: sys_readdir must filter internal cfg: and ns: paths."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from nexus.core.nexus_fs import NexusFS

# ---------------------------------------------------------------------------
# _is_internal_path unit tests
# ---------------------------------------------------------------------------


class TestIsInternalPath:
    """NexusFS._is_internal_path correctly identifies system-internal paths."""

    @pytest.mark.parametrize(
        "path",
        [
            "cfg:search_mutation_checkpoint:bm25",
            "cfg:search_mutation_checkpoint:embedding",
            "cfg:search_mutation_checkpoint:fts",
            "cfg:search_mutation_checkpoint:txtai",
            "ns:rebac:file",
            "ns:rebac:group",
            "ns:rebac:memory",
            "ns:rebac:playbook",
            "ns:rebac:skill",
            "ns:rebac:trajectory",
        ],
    )
    def test_internal_paths_detected(self, path: str) -> None:
        assert NexusFS._is_internal_path(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "/workspace/demo/README.md",
            "/mnt/data.csv",
            "workspace",
            "/workspace",
            "mnt",
            # User paths starting with /cfg: or /ns: must NOT be filtered —
            # only bare keys (no leading slash) are internal metastore entries.
            "/cfg:user-visible",
            "/ns:notes",
            "/cfg:something/nested",
        ],
    )
    def test_user_paths_not_filtered(self, path: str) -> None:
        assert NexusFS._is_internal_path(path) is False


# ---------------------------------------------------------------------------
# sys_readdir integration (mock metadata store)
# ---------------------------------------------------------------------------


@dataclass
class _FakeMeta:
    path: str
    size: int = 0
    content_id: str | None = None
    entry_type: int = 0
    zone_id: str | None = None
    owner_id: str | None = None
    modified_at: object = None
    version: int = 1
    gen: int = 0
    backend_name: str = ""
    physical_path: str = ""


def _build_fs(entries: list[_FakeMeta]) -> NexusFS:
    """Create a NexusFS with a mocked kernel returning *entries*.

    Post-C11 ``sys_readdir`` reaches the metastore via
    ``kernel_helpers.metastore_list_iter(self._kernel, …)`` which
    iterates ``kernel.metastore_list_paginated(prefix, recursive, limit, cursor)``.
    Mock that path.
    """
    kernel = MagicMock()
    kernel.metastore_list_paginated.return_value = {
        "items": list(entries),
        "has_more": False,
        "next_cursor": None,
        "total_count": len(entries),
    }
    # _entry_to_detail_dict calls sys_stat for implicit directory detection
    kernel.sys_stat.return_value = None
    # Force the slow-path branch: when ``readdir`` raises, sys_readdir
    # falls through to ``metastore_list_iter`` which is what these
    # filter-correctness tests are exercising.
    kernel.readdir.side_effect = ValueError("mocked: take slow path")

    fs = object.__new__(NexusFS)
    fs._kernel = kernel
    # NexusFS reads ``self._zone_id`` for zone-scoped listing; default to root.
    from nexus.contracts.constants import ROOT_ZONE_ID
    from nexus.contracts.types import OperationContext

    fs._zone_id = ROOT_ZONE_ID
    # Issue #4081: sys_readdir resolves caller agent_id for OP-event emission
    # via _get_context_identity → _resolve_cred → self._init_cred.
    fs._init_cred = OperationContext(user_id="test", groups=[], zone_id=ROOT_ZONE_ID)
    return fs


class TestSysReaddirInternalFilter:
    """sys_readdir excludes cfg: and ns: entries from results."""

    @pytest.mark.asyncio
    async def test_non_paginated_filters_internal_paths(self) -> None:
        fs = _build_fs(
            [
                _FakeMeta(path="cfg:search_mutation_checkpoint:bm25", entry_type=1),
                _FakeMeta(path="cfg:search_mutation_checkpoint:embedding", entry_type=1),
                _FakeMeta(path="/workspace", entry_type=1),
                _FakeMeta(path="ns:rebac:file", entry_type=1),
                _FakeMeta(path="ns:rebac:group", entry_type=1),
            ]
        )

        result = fs.sys_readdir("/", recursive=False, details=False)

        assert result == ["/workspace"]

    @pytest.mark.asyncio
    async def test_non_paginated_details_filters_internal_paths(self) -> None:
        fs = _build_fs(
            [
                _FakeMeta(path="cfg:search_mutation_checkpoint:bm25", entry_type=1),
                _FakeMeta(path="/workspace", entry_type=1),
                _FakeMeta(path="ns:rebac:file", entry_type=1),
            ]
        )

        result = fs.sys_readdir("/", recursive=False, details=True)

        assert len(result) == 1
        assert result[0]["path"] == "/workspace"

    @pytest.mark.asyncio
    async def test_nonrecursive_details_batches_implicit_dir_detection(self) -> None:
        fs = _build_fs(
            [
                _FakeMeta(path="/workspace/alpha", entry_type=0),
                _FakeMeta(path="/workspace/alpha/file.txt", entry_type=0),
                _FakeMeta(path="/workspace/top.txt", entry_type=0),
            ]
        )
        fs._kernel.metastore_is_implicit_directory.side_effect = AssertionError(
            "nonrecursive details must not probe implicit directories per entry"
        )

        result = fs.sys_readdir("/workspace/", recursive=False, details=True)

        paths = {entry["path"]: entry["entry_type"] for entry in result}
        assert paths == {
            "/workspace/alpha": 1,
            "/workspace/top.txt": 0,
        }
        fs._kernel.metastore_is_implicit_directory.assert_not_called()

    @pytest.mark.asyncio
    async def test_paginated_filters_internal_paths(self) -> None:
        fs = _build_fs(
            [
                _FakeMeta(path="cfg:search_mutation_checkpoint:bm25", entry_type=1),
                _FakeMeta(path="/workspace", entry_type=1),
                _FakeMeta(path="ns:rebac:file", entry_type=1),
            ]
        )

        result = fs.sys_readdir("/", recursive=False, details=False, limit=10)

        assert list(result.items) == ["/workspace"]

    @pytest.mark.asyncio
    async def test_user_paths_not_affected(self) -> None:
        fs = _build_fs(
            [
                _FakeMeta(path="/workspace/demo/README.md"),
                _FakeMeta(path="/workspace/demo/data.csv"),
            ]
        )

        result = fs.sys_readdir("/workspace/demo", recursive=False, details=False)

        assert len(result) == 2


# ---------------------------------------------------------------------------
# sys_readdir zone-column filter (Issue #3779 follow-up)
# ---------------------------------------------------------------------------


class _FakeCtx:
    """Minimal stand-in for OperationContext with only the fields the filter reads."""

    def __init__(self, zone_id: str | None, is_admin: bool = False) -> None:
        self.zone_id = zone_id
        self.is_admin = is_admin
        # Issue #4081: sys_readdir resolves agent_id for OP-event emission;
        # the filter tests don't care about subject identity, but the field
        # must exist.
        self.agent_id = None


class TestSysReaddirZoneFilter:
    """sys_readdir drops rows whose zone_id column doesn't match the caller."""

    @pytest.mark.asyncio
    async def test_own_zone_rows_kept(self) -> None:
        fs = _build_fs(
            [
                _FakeMeta(path="/a.txt", zone_id="zone-a"),
                _FakeMeta(path="/b.txt", zone_id="zone-b"),
            ]
        )
        result = fs.sys_readdir(
            "/", recursive=False, details=False, context=_FakeCtx(zone_id="zone-a")
        )
        assert result == ["/a.txt"]

    @pytest.mark.asyncio
    async def test_sibling_zone_rows_dropped(self) -> None:
        fs = _build_fs(
            [
                _FakeMeta(path="/a.txt", zone_id="zone-a"),
                _FakeMeta(path="/b.txt", zone_id="zone-b"),
                _FakeMeta(path="/c.txt", zone_id="zone-c"),
            ]
        )
        result = fs.sys_readdir(
            "/", recursive=False, details=False, context=_FakeCtx(zone_id="zone-b")
        )
        assert result == ["/b.txt"]

    @pytest.mark.asyncio
    async def test_admin_sees_all_zones(self) -> None:
        fs = _build_fs(
            [
                _FakeMeta(path="/a.txt", zone_id="zone-a"),
                _FakeMeta(path="/b.txt", zone_id="zone-b"),
            ]
        )
        result = fs.sys_readdir(
            "/",
            recursive=False,
            details=False,
            context=_FakeCtx(zone_id="zone-a", is_admin=True),
        )
        assert sorted(result) == ["/a.txt", "/b.txt"]

    @pytest.mark.asyncio
    async def test_root_zone_caller_sees_all(self) -> None:
        from nexus.contracts.constants import ROOT_ZONE_ID

        fs = _build_fs(
            [
                _FakeMeta(path="/a.txt", zone_id="zone-a"),
                _FakeMeta(path="/b.txt", zone_id=ROOT_ZONE_ID),
            ]
        )
        result = fs.sys_readdir(
            "/", recursive=False, details=False, context=_FakeCtx(zone_id=ROOT_ZONE_ID)
        )
        assert sorted(result) == ["/a.txt", "/b.txt"]

    @pytest.mark.asyncio
    async def test_paginated_applies_zone_filter(self) -> None:
        fs = _build_fs(
            [
                _FakeMeta(path="/a.txt", zone_id="zone-a"),
                _FakeMeta(path="/b.txt", zone_id="zone-b"),
                _FakeMeta(path="/c.txt", zone_id="zone-a"),
            ]
        )
        result = fs.sys_readdir(
            "/",
            recursive=False,
            details=False,
            limit=10,
            context=_FakeCtx(zone_id="zone-a"),
        )
        assert sorted(result.items) == ["/a.txt", "/c.txt"]

    @pytest.mark.asyncio
    async def test_dict_context_applies_zone_filter(self) -> None:
        """Dict-shaped context (some RPC paths) must be handled the same as OperationContext."""
        fs = _build_fs(
            [
                _FakeMeta(path="/a.txt", zone_id="zone-a"),
                _FakeMeta(path="/b.txt", zone_id="zone-b"),
            ]
        )
        result = fs.sys_readdir(
            "/",
            recursive=False,
            details=False,
            context={"zone_id": "zone-a", "is_admin": False},
        )
        assert result == ["/a.txt"]
