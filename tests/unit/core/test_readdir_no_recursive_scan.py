"""sys_readdir(details=True) without ``limit`` must not scan the subtree (#4777).

The unpaginated details listing used to call ``metastore_list_paginated``
with ``recursive=True`` on the whole subtree to decide which direct children
have descendants — O(every file below the directory) per listing: 54 s for a
5-child directory over ~7k descendants locally, 764 s for ``/workspaces`` on
a production corpus.  The kernel's non-recursive readdir already reports
directories as DT_DIR; the remaining DT_REG children are confirmed with one
batched stat.
"""

from __future__ import annotations

from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.metadata import DT_DIR, FileMetadata
from nexus.core.nexus_fs_metadata import MetadataMixin

PARENT = "/workspaces"
DIRS = [f"{PARENT}/ws-{i:03d}" for i in range(5)]
LOOSE_FILE = f"{PARENT}/README.md"
IMPLICIT_DIR_AS_REG = f"{PARENT}/legacy"  # readdir says DT_REG, stat says directory
DESCENDANTS = [f"{d}/documents/doc-{k}.md" for d in DIRS for k in range(20)]


class _KernelNoBatch:
    """Fake kernel WITHOUT ``stat_batch`` (older kernel clients / test doubles)."""

    def __init__(self) -> None:
        self.list_calls: list[tuple[str, bool]] = []
        self.sys_stat_calls: list[str] = []
        self.children = (
            [FileMetadata(path=d, size=0, entry_type=DT_DIR) for d in DIRS]
            + [FileMetadata(path=LOOSE_FILE, size=3, entry_type=0)]
            + [FileMetadata(path=IMPLICIT_DIR_AS_REG, size=0, entry_type=0)]
        )
        self.everything = self.children + [
            FileMetadata(path=p, size=1, entry_type=0) for p in DESCENDANTS
        ]

    def metastore_list_paginated(
        self, prefix: str, recursive: bool, limit: int, cursor: Any
    ) -> dict[str, Any]:
        self.list_calls.append((prefix, recursive))
        items = self.everything if recursive else self.children
        items = [e for e in items if e.path.startswith(prefix)]
        if cursor:
            items = [e for e in items if e.path > cursor]
        page = items[:limit]
        return {
            "items": page,
            "next_cursor": page[-1].path if len(items) > limit else None,
            "has_more": len(items) > limit,
            "total_count": len(items),
        }

    def sys_stat(self, path: str, zone_id: str = ROOT_ZONE_ID) -> dict[str, Any] | None:
        self.sys_stat_calls.append(path)
        return {"path": path, "is_directory": path == IMPLICIT_DIR_AS_REG, "zone_id": None}


class _KernelWithBatch(_KernelNoBatch):
    def __init__(self) -> None:
        super().__init__()
        self.stat_batch_calls: list[list[str]] = []

    def stat_batch(self, paths: list[str], zone_id: str = ROOT_ZONE_ID) -> list[Any]:
        self.stat_batch_calls.append(list(paths))
        return [
            {"path": p, "is_directory": p == IMPLICIT_DIR_AS_REG, "zone_id": None} for p in paths
        ]


class _FakeFS(MetadataMixin):
    def __init__(self, kernel: Any) -> None:
        self._kernel = kernel
        self._zone_id = ROOT_ZONE_ID
        self._hook_specs: dict[str, Any] = {}
        self.metadata = None
        self._driver_coordinator = None
        self._init_cred: Any = None

    def _get_context_identity(self, context: Any = None) -> tuple[str | None, str | None, bool]:
        return (ROOT_ZONE_ID, None, True)

    def sys_stat(self, path: str, *, context: Any = None, **_kwargs: Any) -> dict[str, Any] | None:
        result: dict[str, Any] | None = self._kernel.sys_stat(path, ROOT_ZONE_ID)
        return result


def _assert_projection(rows: Any) -> None:
    by_path = {r["path"]: r for r in rows}
    assert set(by_path) == set(DIRS) | {LOOSE_FILE, IMPLICIT_DIR_AS_REG}
    # Directories keep DT_DIR; the DT_REG-typed implicit directory is
    # promoted; the loose file stays a file.
    assert all(by_path[d]["entry_type"] == DT_DIR for d in DIRS)
    assert by_path[IMPLICIT_DIR_AS_REG]["entry_type"] == DT_DIR
    assert by_path[LOOSE_FILE]["entry_type"] == 0


def test_unpaginated_details_listing_reads_direct_children_only() -> None:
    kernel = _KernelWithBatch()
    rows = _FakeFS(kernel).sys_readdir(PARENT, recursive=False, details=True, context=None)
    _assert_projection(rows)
    assert all(not recursive for _, recursive in kernel.list_calls), (
        f"listing must never scan the subtree: {kernel.list_calls}"
    )
    # One batched stat covering exactly the DT_REG children, no per-entry stats.
    assert kernel.stat_batch_calls == [[LOOSE_FILE, IMPLICIT_DIR_AS_REG]]
    assert kernel.sys_stat_calls == []


def test_falls_back_to_per_entry_stat_without_stat_batch() -> None:
    kernel = _KernelNoBatch()
    rows = _FakeFS(kernel).sys_readdir(PARENT, recursive=False, details=True, context=None)
    _assert_projection(rows)
    assert all(not recursive for _, recursive in kernel.list_calls)
    assert set(kernel.sys_stat_calls) == {LOOSE_FILE, IMPLICIT_DIR_AS_REG}
