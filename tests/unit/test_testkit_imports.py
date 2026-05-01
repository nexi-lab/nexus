"""Tests for the canonical tests.testkit package."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path


def test_common_helpers_exported_from_testkit() -> None:
    from tests.testkit import (
        TEST_ADMIN_CONTEXT,
        TEST_CONTEXT,
        DictMetastore,
        FailingBackend,
        FailingMetastore,
        InMemoryNexusFS,
        InMemoryRecordStore,
        MetastoreError,
        MockWebSocket,
        make_test_nexus,
        operation_context,
    )

    assert callable(DictMetastore)
    assert FailingBackend.__name__ == "FailingBackend"
    assert FailingMetastore.__name__ == "FailingMetastore"
    assert InMemoryNexusFS.__name__ == "InMemoryNexusFS"
    assert InMemoryRecordStore.__name__ == "InMemoryRecordStore"
    assert MetastoreError.__name__ == "MetastoreError"
    assert MockWebSocket.__name__ == "MockWebSocket"
    assert TEST_CONTEXT.user_id == "test"
    assert TEST_ADMIN_CONTEXT.is_admin is True
    assert callable(make_test_nexus)
    assert callable(operation_context)


def test_compatibility_imports_point_to_canonical_objects() -> None:
    from tests.testkit import (
        TEST_CONTEXT,
        DictMetastore,
        FailingBackend,
        FailingMetastore,
        InMemoryNexusFS,
        InMemoryRecordStore,
        MetastoreError,
        MockWebSocket,
        make_test_nexus,
        operation_context,
    )

    conftest = importlib.import_module("tests.conftest")
    dict_metastore = importlib.import_module("tests.helpers.dict_metastore")
    failing_backend = importlib.import_module("tests.helpers.failing_backend")
    failing_metastore = importlib.import_module("tests.helpers.failing_metastore")
    record_store = importlib.import_module("tests.helpers.in_memory_record_store")
    nexus_fs = importlib.import_module("tests.helpers.inmemory_nexus_fs")
    mock_websocket = importlib.import_module("tests.helpers.mock_websocket")
    test_context = importlib.import_module("tests.helpers.test_context")

    assert dict_metastore.DictMetastore is DictMetastore
    assert failing_backend.FailingBackend is FailingBackend
    assert failing_metastore.FailingMetastore is FailingMetastore
    assert failing_metastore.MetastoreError is MetastoreError
    assert record_store.InMemoryRecordStore is InMemoryRecordStore
    assert nexus_fs.InMemoryNexusFS is InMemoryNexusFS
    assert mock_websocket.MockWebSocket is MockWebSocket
    assert test_context.TEST_CONTEXT is TEST_CONTEXT
    assert test_context.operation_context is operation_context
    assert conftest.make_test_nexus is make_test_nexus


def test_top_level_helpers_import_spelling_points_to_testkit() -> None:
    from tests.testkit import MockWebSocket

    mock_websocket = importlib.import_module("helpers.mock_websocket")

    assert mock_websocket.MockWebSocket is MockWebSocket


def test_metadata_module_defines_public_interface() -> None:
    from tests.testkit.metadata import __all__

    assert __all__ == [
        "DictMetastore",
        "FailingMetastore",
        "InMemoryNexusFS",
        "MetastoreError",
    ]


def test_operation_context_factory_builds_explicit_identity() -> None:
    from tests.testkit import operation_context

    context = operation_context(
        user_id="alice",
        groups=("eng", "qa"),
        zone_id="zone-a",
        is_system=True,
        is_admin=True,
    )

    assert context.user_id == "alice"
    assert context.groups == ["eng", "qa"]
    assert context.zone_id == "zone-a"
    assert context.is_system is True
    assert context.is_admin is True


def test_dict_metastore_factory_returns_usable_store(tmp_path: Path) -> None:
    from tests.testkit import DictMetastore

    store = DictMetastore(tmp_path / "metadata.redb")
    try:
        assert hasattr(store, "get")
        assert hasattr(store, "put")
        assert store.get("/missing") is None
    finally:
        store.close()


def test_make_test_nexus_export_has_stable_name() -> None:
    from tests.testkit import make_test_nexus

    assert isinstance(make_test_nexus, Callable)
    assert make_test_nexus.__name__ == "make_test_nexus"


def test_testkit_fixture_functions_are_importable() -> None:
    from tests.testkit.fixtures import isolated_db, record_store

    assert isolated_db.__name__ == "isolated_db"
    assert record_store.__name__ == "record_store"
