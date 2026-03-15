"""Tests for the JSON-backed DictMetastore fallback."""

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


def test_dict_metastore_persists_and_supports_numeric_pagination_cursor(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "metastore.json"

    store = DictMetastore(storage_path)
    store.put(_metadata("/workspace/a.txt"))
    store.put(_metadata("/workspace/b.txt"))
    store.close()

    reopened = DictMetastore(storage_path)
    first_page = reopened.list_paginated(prefix="/workspace", limit=1)

    assert [item.path for item in first_page.items] == ["/workspace/a.txt"]
    assert first_page.next_cursor == "1"

    second_page = reopened.list_paginated(
        prefix="/workspace",
        limit=1,
        cursor=first_page.next_cursor,
    )

    assert [item.path for item in second_page.items] == ["/workspace/b.txt"]
    assert second_page.next_cursor is None
    reopened.close()
