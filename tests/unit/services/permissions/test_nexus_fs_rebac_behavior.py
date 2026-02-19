"""Behavior tests for NexusFS ReBAC-related utility methods.

Tests cover context extraction (_get_subject_from_context).

Note: Core ReBAC operations (rebac_create, rebac_check, rebac_delete, etc.)
are now served by ReBACService and tested in test_rebac_service.py.
"""

from unittest.mock import MagicMock, Mock

import pytest

from nexus.core.nexus_fs import NexusFS


class MockNexusFS:
    """Test fixture that provides NexusFS._get_subject_from_context for testing."""

    def __init__(self, rebac_manager=None, enforce_permissions=True):
        self._rebac_manager = rebac_manager
        self._enforce_permissions = enforce_permissions

    # Bind remaining method from NexusFS
    _get_subject_from_context = NexusFS._get_subject_from_context


class TestGetSubjectFromContext:
    """Tests for _get_subject_from_context method."""

    def test_dict_with_subject_key(self):
        """Extracts subject from dict with 'subject' key."""
        fs = MockNexusFS()
        context = {"subject": ("user", "alice")}

        result = fs._get_subject_from_context(context)

        assert result == ("user", "alice")

    def test_dict_with_subject_type_and_id(self):
        """Constructs subject from dict with 'subject_type' and 'subject_id' keys."""
        fs = MockNexusFS()
        context = {"subject_type": "agent", "subject_id": "bob"}

        result = fs._get_subject_from_context(context)

        assert result == ("agent", "bob")

    def test_dict_with_user_id_key(self):
        """Extracts from 'user_id' key in dict when subject fields are missing."""
        fs = MockNexusFS()
        context = {"user_id": "charlie"}

        result = fs._get_subject_from_context(context)

        assert result == ("user", "charlie")

    def test_dict_with_subject_type_without_id_uses_user_id(self):
        """Uses 'user_id' field as subject_id when subject_id is missing."""
        fs = MockNexusFS()
        context = {"subject_type": "agent", "user_id": "dave"}

        result = fs._get_subject_from_context(context)

        assert result == ("agent", "dave")

    def test_operation_context_with_get_subject_method(self):
        """Extracts subject using get_subject() method from context object."""
        fs = MockNexusFS()
        mock_context = Mock()
        mock_context.get_subject = Mock(return_value=("group", "developers"))

        result = fs._get_subject_from_context(mock_context)

        assert result == ("group", "developers")
        mock_context.get_subject.assert_called_once()

    def test_operation_context_get_subject_returns_none(self):
        """Returns None when get_subject() method returns None."""
        fs = MockNexusFS()
        mock_context = Mock()
        mock_context.get_subject = Mock(return_value=None)

        result = fs._get_subject_from_context(mock_context)

        assert result is None

    def test_object_with_subject_type_and_id_attributes(self):
        """Extracts subject from object with subject_type and subject_id attributes."""
        fs = MockNexusFS()
        mock_context = Mock(spec=[])
        mock_context.subject_type = "workspace"
        mock_context.subject_id = "project1"

        result = fs._get_subject_from_context(mock_context)

        assert result == ("workspace", "project1")

    def test_object_with_user_attribute(self):
        """Extracts subject from object with only user attribute."""
        fs = MockNexusFS()
        mock_context = Mock(spec=["user"])
        mock_context.user_id = "eve"

        result = fs._get_subject_from_context(mock_context)

        assert result == ("user", "eve")

    def test_none_context_returns_none(self):
        """Returns None when context is None."""
        fs = MockNexusFS()

        result = fs._get_subject_from_context(None)

        assert result is None

    def test_empty_dict_returns_none(self):
        """Returns None when context is empty dict."""
        fs = MockNexusFS()
        context = {}

        result = fs._get_subject_from_context(context)

        assert result is None
