"""Streaming a recursive listing through the gRPC KernelClient walks the tree ONCE.

``metastore_list_iter`` paged through ``KernelClient.metastore_list_paginated``
with a cursor, and every page re-walked the whole tree (one ``sys_readdir``
RPC per directory) and re-sorted it — quadratic in the entry count.  The
boot-time Tiger resource-map sync lists ``/`` recursively; at ~155k files on
Koodle's production zone it ran for most of 40 minutes.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from nexus.contracts.metadata import DT_DIR
from nexus.kernel_helpers import metastore_list_iter
from nexus.remote.kernel_client import KernelClient

DIRS = [f"/ws/d{i:02d}" for i in range(12)]
FILES = [f"{d}/f{k:03d}.md" for d in DIRS for k in range(250)]  # 3,000 files


class _FakeKernelClient(KernelClient):
    def __init__(self) -> None:  # no subprocess / transport
        self.readdir_calls = 0
        self.stat_batch_sizes: list[int] = []

    def sys_readdir(self, path: str, *args: Any, **kwargs: Any) -> list[tuple[str, int]]:
        self.readdir_calls += 1
        if path == "/":
            return [("/ws", DT_DIR)]
        if path == "/ws":
            return [(d, DT_DIR) for d in DIRS]
        return [(f, 0) for f in FILES if f.startswith(path + "/")]

    def stat_batch(self, paths: list[str], zone_id: str = "root") -> list[Any]:
        self.stat_batch_sizes.append(len(paths))
        return [{"path": p, "size": 1, "entry_type": 0, "zone_id": None} for p in paths]


def test_recursive_listing_walks_the_tree_once() -> None:
    kernel = _FakeKernelClient()
    paths = [e.path for e in metastore_list_iter(kernel, prefix="", recursive=True)]

    assert paths == sorted(["/ws", *DIRS, *FILES])
    # One readdir per directory (/, /ws, 12 dirs) — not once per 1,000-entry page.
    assert kernel.readdir_calls == 1 + 1 + len(DIRS)
    # Metadata still comes in bounded stat_batch chunks.
    assert kernel.stat_batch_sizes == [1000, 1000, 1000, 13]


def test_paginated_list_keeps_its_contract() -> None:
    kernel = _FakeKernelClient()
    first = kernel.metastore_list_paginated("", True, 1000, None)
    second = kernel.metastore_list_paginated("", True, 1000, first["next_cursor"])
    assert first["has_more"] and first["total_count"] == 1 + len(DIRS) + len(FILES)
    assert len(first["items"]) == 1000
    assert second["items"][0].path > first["items"][-1].path


def test_mock_kernels_keep_the_paginated_path() -> None:
    kernel = MagicMock()
    kernel.metastore_list_paginated.return_value = {
        "items": [MagicMock(path="/a")],
        "next_cursor": None,
        "has_more": False,
        "total_count": 1,
    }
    assert [e.path for e in metastore_list_iter(kernel, "", True)] == ["/a"]
    kernel.metastore_list_paginated.assert_called_once()
