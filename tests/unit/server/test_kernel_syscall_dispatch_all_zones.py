"""RPC list result adapter keeps zone-qualified paths for ``all_zones`` (#4740).

Single-zone callers get user-facing paths (``/zone/<id>/`` stripped) as
before; an admin's explicit cross-zone listing keeps the prefix, otherwise
``/zone/ta/a.txt`` and ``/zone/tb/a.txt`` would collapse into one ``/a.txt``.
"""

from __future__ import annotations

from nexus.core.pagination import PaginatedResult
from nexus.server._kernel_syscall_dispatch import _apply_result_adapter

_ENTRIES = ["/zone/ta/a.txt", "/zone/tb/a.txt", "/root.txt"]
_DETAIL_ENTRIES = [{"path": p, "size": 1} for p in _ENTRIES]


def test_single_zone_list_unscopes_paths() -> None:
    wire = _apply_result_adapter("list", list(_ENTRIES), {"path": "/"})
    assert wire["files"] == ["/a.txt", "/a.txt", "/root.txt"]
    assert wire["has_more"] is False


def test_all_zones_list_keeps_zone_prefix() -> None:
    wire = _apply_result_adapter("sys_readdir", list(_ENTRIES), {"path": "/", "all_zones": True})
    assert wire["files"] == _ENTRIES


def test_all_zones_detail_dicts_keep_zone_prefix() -> None:
    wire = _apply_result_adapter("list", list(_DETAIL_ENTRIES), {"all_zones": True})
    assert [f["path"] for f in wire["files"]] == _ENTRIES

    wire_scoped = _apply_result_adapter("list", list(_DETAIL_ENTRIES), {"all_zones": False})
    assert [f["path"] for f in wire_scoped["files"]] == ["/a.txt", "/a.txt", "/root.txt"]


def test_all_zones_paginated_result_keeps_zone_prefix() -> None:
    page = PaginatedResult(items=list(_ENTRIES), next_cursor="/root.txt", has_more=True)
    wire = _apply_result_adapter("list", page, {"all_zones": True, "limit": 3})
    assert wire["files"] == _ENTRIES
    assert wire["has_more"] is True
    assert wire["next_cursor"] == "/root.txt"

    wire_scoped = _apply_result_adapter("list", page, {"limit": 3})
    assert wire_scoped["files"] == ["/a.txt", "/a.txt", "/root.txt"]
