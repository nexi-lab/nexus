"""Recursive sys_readdir expansion must not stat every entry or re-list every directory.

``_expand_recursive_readdir`` descends into directories whose entries live
behind their own metastore route.  It used to ``sys_stat`` every bare-string
entry to learn whether it was a directory and then re-list EVERY directory's
subtree, although a plain directory's descendants already came back from the
same prefix scan: ``sys_readdir("/", recursive=True)`` took ~34 min for ~150k
files on a production zone — the Tiger resource-map sync runs exactly that on
every boot.
"""

from __future__ import annotations

from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.metadata import DT_DIR, DT_MOUNT, FileMetadata
from nexus.core.nexus_fs_metadata import MetadataMixin

WORKSPACES = [f"/workspaces/ws-{i:03d}" for i in range(30)]
MOUNT = "/mnt/drive"
ROUTED_DIR = "/routed"  # explicit DT_DIR whose children live in another route
HIDDEN = {
    MOUNT: [f"{MOUNT}/a.txt", f"{MOUNT}/sub/b.txt"],
    ROUTED_DIR: [f"{ROUTED_DIR}/c.txt"],
}


class _Kernel:
    def __init__(self) -> None:
        self.list_calls: list[tuple[str, bool]] = []
        self.sys_stat_calls: list[str] = []
        visible = [FileMetadata(path=w, size=0, entry_type=DT_DIR) for w in WORKSPACES]
        visible += [
            FileMetadata(path=f"{w}/documents", size=0, entry_type=DT_DIR) for w in WORKSPACES
        ]
        visible += [
            FileMetadata(path=f"{w}/documents/doc-{k}.md", size=1, entry_type=0)
            for w in WORKSPACES
            for k in range(10)
        ]
        visible += [
            FileMetadata(path=MOUNT, size=0, entry_type=DT_MOUNT),
            FileMetadata(path=ROUTED_DIR, size=0, entry_type=DT_DIR),
        ]
        self.visible = visible
        self.routes = {
            root: [FileMetadata(path=p, size=1, entry_type=0) for p in paths]
            for root, paths in HIDDEN.items()
        }

    def metastore_list_paginated(
        self, prefix: str, recursive: bool, limit: int, cursor: Any
    ) -> dict[str, Any]:
        self.list_calls.append((prefix, recursive))
        route = next((r for r in self.routes if prefix.startswith(r + "/")), None)
        items = self.routes[route] if route else self.visible
        items = [e for e in items if e.path.startswith(prefix)]
        if not recursive:
            depth = prefix.count("/")
            items = [e for e in items if e.path.count("/") == depth]
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
        is_dir = any(e.path == path and e.entry_type in (DT_DIR, DT_MOUNT) for e in self.visible)
        return {"path": path, "is_directory": is_dir, "zone_id": None}


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


def _expected_paths() -> set[str]:
    kernel = _Kernel()
    out = {e.path for e in kernel.visible}
    for paths in HIDDEN.values():
        out.update(paths)
    return out


def test_recursive_listing_keeps_routed_children_without_stats_or_relisting() -> None:
    kernel = _Kernel()
    paths = _FakeFS(kernel).sys_readdir("/", recursive=True, details=False, context=None)

    assert set(paths) == _expected_paths()
    assert paths == sorted(paths)
    assert kernel.sys_stat_calls == [], (
        "directory-ness comes from entry types, not a stat per entry"
    )
    recursive_prefixes = sorted(p for p, recursive in kernel.list_calls if recursive)
    # The root scan plus exactly the two directories that can hide entries.
    # Before: one nested recursive listing per workspace AND per documents dir.
    assert recursive_prefixes == sorted(["", f"{MOUNT}/", f"{ROUTED_DIR}/"])


def test_recursive_details_listing_expands_the_same_routes() -> None:
    kernel = _Kernel()
    rows = _FakeFS(kernel).sys_readdir("/", recursive=True, details=True, context=None)

    assert {r["path"] for r in rows} == _expected_paths()
    assert all(isinstance(r, dict) for r in rows)
    assert kernel.sys_stat_calls == []
    recursive_prefixes = sorted(p for p, recursive in kernel.list_calls if recursive)
    assert recursive_prefixes == sorted(["", f"{MOUNT}/", f"{ROUTED_DIR}/"])
