"""Tests for the ``DictMetastore`` compat factory (F3 C5).

The pre-F3 JSON-backed class has been replaced with a thin factory
function that returns a ``RustMetastoreProxy`` — see
``src/nexus/storage/dict_metastore.py``. These tests exercise the
two behaviours the old class test cared about:

  1. Puts are visible on subsequent reads within the same proxy.
  2. ``list_paginated`` returns a numeric cursor that advances to the
     next page.

The old ``close() + reopen-same-file`` round-trip is no longer
meaningful — redb holds an exclusive process-wide lock on its file,
so close-and-reopen within one Python process is a platform
anti-pattern for the durable backend. A dedicated cross-process
durability test would belong in the integration suite, not here.
"""

from __future__ import annotations

from pathlib import Path

from nexus.contracts.metadata import FileMetadata
from nexus.storage.dict_metastore import DictMetastore


def _metadata(path: str) -> FileMetadata:
    return FileMetadata(
        path=path,
        backend_name="local",
        physical_path=f"cas:{path}",
        size=1,
    )


def test_dict_metastore_supports_numeric_pagination_cursor(tmp_path: Path) -> None:
    storage_path = tmp_path / "metastore.json"
    store = DictMetastore(storage_path)
    store.put(_metadata("/workspace/a.txt"))
    store.put(_metadata("/workspace/b.txt"))

    first_page = store.list_paginated(prefix="/workspace", limit=1)
    assert [item.path for item in first_page.items] == ["/workspace/a.txt"]
    assert first_page.next_cursor == "1"

    second_page = store.list_paginated(
        prefix="/workspace",
        limit=1,
        cursor=first_page.next_cursor,
    )
    assert [item.path for item in second_page.items] == ["/workspace/b.txt"]
    assert second_page.next_cursor is None
    store.close()
