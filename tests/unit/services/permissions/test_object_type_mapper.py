"""Tests for ObjectTypeMapper — ReBAC object type mapping extracted from Backend.

TDD: Tests written first per Decision 10.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.services.permissions.object_type_mapper import ObjectTypeMapper


@pytest.fixture
def mapper() -> ObjectTypeMapper:
    return ObjectTypeMapper()


class TestGetObjectType:
    def test_default_mapping_returns_file(self, mapper: ObjectTypeMapper) -> None:
        """Default backend returns 'file' for any path."""
        backend = MagicMock()
        backend.get_object_type.return_value = "file"
        assert mapper.get_object_type(backend, "some/path.txt") == "file"

    def test_backend_specific_override(self, mapper: ObjectTypeMapper) -> None:
        """Backend can return custom object types (e.g., 'ipc:agent')."""
        backend = MagicMock()
        backend.get_object_type.return_value = "ipc:agent"
        assert mapper.get_object_type(backend, "agents/AGENT.json") == "ipc:agent"

    def test_fallback_on_exception(self, mapper: ObjectTypeMapper) -> None:
        """If backend.get_object_type raises, fall back to 'file'."""
        backend = MagicMock()
        backend.get_object_type.side_effect = Exception("boom")
        assert mapper.get_object_type(backend, "broken/path") == "file"


class TestGetObjectId:
    def test_file_type_uses_virtual_path(self, mapper: ObjectTypeMapper) -> None:
        """For file objects, the virtual path is used as object ID."""
        backend = MagicMock()
        result = mapper.get_object_id(
            backend,
            backend_path="data/file.csv",
            virtual_path="/mnt/gcs/data/file.csv",
            object_type="file",
        )
        assert result == "/mnt/gcs/data/file.csv"

    def test_non_file_type_delegates_to_backend(self, mapper: ObjectTypeMapper) -> None:
        """For non-file objects, delegate to backend.get_object_id()."""
        backend = MagicMock()
        backend.get_object_id.return_value = "agents/my-agent"
        result = mapper.get_object_id(
            backend,
            backend_path="agents/AGENT.json",
            virtual_path="/ipc/agents/AGENT.json",
            object_type="ipc:agent",
        )
        assert result == "agents/my-agent"
        backend.get_object_id.assert_called_once_with("agents/AGENT.json")

    def test_non_file_fallback_on_exception(self, mapper: ObjectTypeMapper) -> None:
        """If backend.get_object_id raises for non-file, fall back to virtual_path."""
        backend = MagicMock()
        backend.get_object_id.side_effect = Exception("boom")
        result = mapper.get_object_id(
            backend,
            backend_path="broken",
            virtual_path="/mnt/broken",
            object_type="custom:type",
        )
        assert result == "/mnt/broken"
