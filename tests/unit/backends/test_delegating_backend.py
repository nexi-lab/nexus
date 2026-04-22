"""Unit tests for DelegatingBackend property forwarding (#1449).

Verifies that all 11 Backend capability properties are delegated to the
inner backend rather than returning Backend defaults.

Design reference:
    - NEXUS-LEGO-ARCHITECTURE.md PART 16, Recursive Wrapping Rule #2
"""

from unittest.mock import MagicMock, PropertyMock

import pytest

from nexus.backends.base.backend import Backend
from nexus.backends.storage.delegating import DelegatingBackend
from nexus.core.object_store import WriteResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_inner() -> MagicMock:
    """Create a mock Backend with all properties returning non-default values."""
    mock = MagicMock(spec=Backend)
    mock.name = "test-backend"
    mock.describe.return_value = "test-backend"
    # Set all capability flags to True (opposite of Backend defaults)
    type(mock).is_connected = PropertyMock(return_value=True)
    type(mock).thread_safe = PropertyMock(return_value=True)
    type(mock).supports_rename = PropertyMock(return_value=True)
    type(mock).has_root_path = PropertyMock(return_value=True)
    type(mock).has_data_dir = PropertyMock(return_value=True)
    type(mock).supports_parallel_mmap_read = PropertyMock(return_value=True)
    return mock


@pytest.fixture
def delegating(mock_inner: MagicMock) -> DelegatingBackend:
    """Create a DelegatingBackend wrapping the mock inner."""
    return DelegatingBackend(inner=mock_inner)


# ---------------------------------------------------------------------------
# Property Delegation Tests
# ---------------------------------------------------------------------------


class TestPropertyDelegation:
    """All Backend capability properties must forward to inner, not return defaults."""

    @pytest.mark.parametrize(
        "prop",
        [
            "is_connected",
            "thread_safe",
            "supports_rename",
            "has_root_path",
            "has_data_dir",
            "supports_parallel_mmap_read",
        ],
    )
    def test_property_delegates_to_inner(self, delegating: DelegatingBackend, prop: str) -> None:
        """Each property should return the inner backend's value, not Backend default."""
        assert getattr(delegating, prop) is True

    def test_name_delegates(self, delegating: DelegatingBackend) -> None:
        assert delegating.name == "test-backend"


# ---------------------------------------------------------------------------
# describe() Tests
# ---------------------------------------------------------------------------


class TestDescribeDelegation:
    """DelegatingBackend.describe() should pass through to inner by default."""

    def test_describe_passthrough(self, delegating: DelegatingBackend) -> None:
        assert delegating.describe() == "test-backend"

    def test_describe_chains_with_inner(self, mock_inner: MagicMock) -> None:
        """When inner is also a wrapper, describe() should return inner's full chain."""
        mock_inner.describe.return_value = "cache -> s3"
        wrapper = DelegatingBackend(inner=mock_inner)
        assert wrapper.describe() == "cache -> s3"


# ---------------------------------------------------------------------------
# Content Operation Delegation Tests
# ---------------------------------------------------------------------------


class TestContentDelegation:
    """Content operations should delegate to inner backend."""

    def test_read_content(self, delegating: DelegatingBackend, mock_inner: MagicMock) -> None:
        expected = b"hello"
        mock_inner.read_content.return_value = expected
        result = delegating.read_content("abc123")
        mock_inner.read_content.assert_called_once_with("abc123", context=None)
        assert result is expected

    def test_write_content(self, delegating: DelegatingBackend, mock_inner: MagicMock) -> None:
        expected = WriteResult(content_id="hash123", size=4)
        mock_inner.write_content.return_value = expected
        result = delegating.write_content(b"data")
        mock_inner.write_content.assert_called_once_with(b"data", "", offset=0, context=None)
        assert result is expected

    def test_delete_content(self, delegating: DelegatingBackend, mock_inner: MagicMock) -> None:
        mock_inner.delete_content.return_value = None
        result = delegating.delete_content("abc123")
        mock_inner.delete_content.assert_called_once_with("abc123", context=None)
        assert result is None


# ---------------------------------------------------------------------------
# Directory Operation Delegation Tests
# ---------------------------------------------------------------------------


class TestDirectoryDelegation:
    """Directory operations should delegate to inner backend."""

    def test_mkdir(self, delegating: DelegatingBackend, mock_inner: MagicMock) -> None:
        mock_inner.mkdir.return_value = None
        result = delegating.mkdir("/test", parents=True, exist_ok=True)
        mock_inner.mkdir.assert_called_once_with("/test", parents=True, exist_ok=True, context=None)
        assert result is None

    def test_rmdir(self, delegating: DelegatingBackend, mock_inner: MagicMock) -> None:
        mock_inner.rmdir.return_value = None
        result = delegating.rmdir("/test", recursive=True)
        mock_inner.rmdir.assert_called_once_with("/test", recursive=True, context=None)
        assert result is None


# ---------------------------------------------------------------------------
# __getattr__ Fallback Tests
# ---------------------------------------------------------------------------


class TestListDirDelegation:
    """list_dir has a NotImplementedError default on Backend, must explicitly delegate."""

    def test_list_dir_delegates(self, delegating: DelegatingBackend, mock_inner: MagicMock) -> None:
        mock_inner.list_dir.return_value = ["file.txt", "subdir/"]
        result = delegating.list_dir("/path")
        mock_inner.list_dir.assert_called_once_with("/path", context=None)
        assert result == ["file.txt", "subdir/"]


class TestGetAttrFallback:
    """__getattr__ should forward truly unknown attributes to inner backend."""

    def test_getattr_delegates_unknown_attribute(
        self, delegating: DelegatingBackend, mock_inner: MagicMock
    ) -> None:
        mock_inner.some_custom_attr = "custom_value"
        assert delegating.some_custom_attr == "custom_value"

    def test_getattr_delegates_unknown_method(self) -> None:
        """Use a non-spec mock to verify __getattr__ forwards unknown methods."""
        inner = MagicMock()  # No spec -- allows arbitrary attributes
        inner.name = "flexible"
        inner.describe.return_value = "flexible"
        wrapper = DelegatingBackend(inner=inner)
        inner.some_future_method.return_value = 42
        assert wrapper.some_future_method() == 42
