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
    etag: str | None = None
    entry_type: int = 0
    zone_id: str | None = None
    owner_id: str | None = None
    modified_at: object = None
    version: int = 1
    backend_name: str = ""
    physical_path: str = ""


def _build_fs(entries: list[_FakeMeta]) -> NexusFS:
    """Create a NexusFS with a mocked metadata store returning *entries*."""
    meta = MagicMock()
    meta.list.return_value = entries
    meta.list_iter.return_value = iter(entries)
    meta.is_implicit_directory.return_value = False

    fs = object.__new__(NexusFS)
    fs.metadata = meta
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
