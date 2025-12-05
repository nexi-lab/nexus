"""Unit tests for HackerNews connector backend."""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from nexus.backends.hn_connector import (
    HNConnectorBackend,
)
from nexus.core.exceptions import BackendError
from nexus.core.permissions import OperationContext


@pytest.fixture
def hn_connector() -> HNConnectorBackend:
    """Create an HN connector instance."""
    return HNConnectorBackend(
        cache_ttl=300,
        stories_per_feed=10,
        include_comments=True,
    )


@pytest.fixture
def mock_httpx_client():
    """Create a mock httpx client."""
    with patch("nexus.backends.hn_connector.httpx.AsyncClient") as mock_class:
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_class.return_value = mock_client
        yield mock_client


@pytest.fixture
def sample_story() -> dict:
    """Sample HN story response."""
    return {
        "id": 12345,
        "type": "story",
        "title": "Show HN: Test Project",
        "url": "https://example.com/project",
        "text": None,
        "by": "testuser",
        "time": 1701619200,
        "score": 142,
        "descendants": 3,
        "kids": [12346, 12347],
    }


@pytest.fixture
def sample_comment() -> dict:
    """Sample HN comment response."""
    return {
        "id": 12346,
        "type": "comment",
        "by": "commenter1",
        "text": "Great project!",
        "time": 1701620000,
        "parent": 12345,
        "kids": [12348],
    }


class TestHNConnectorInitialization:
    """Test HN connector initialization."""

    def test_init_defaults(self) -> None:
        """Test initialization with defaults."""
        backend = HNConnectorBackend()

        assert backend.name == "hn"
        assert backend.cache_ttl == 300
        assert backend.stories_per_feed == 10
        assert backend.include_comments is True
        assert backend.user_scoped is False

    def test_init_custom_values(self) -> None:
        """Test initialization with custom values."""
        backend = HNConnectorBackend(
            cache_ttl=600,
            stories_per_feed=20,
            include_comments=False,
        )

        assert backend.cache_ttl == 600
        assert backend.stories_per_feed == 20
        assert backend.include_comments is False

    def test_init_stories_per_feed_clamped(self) -> None:
        """Test stories_per_feed is clamped to valid range."""
        # Too low
        backend = HNConnectorBackend(stories_per_feed=0)
        assert backend.stories_per_feed == 1

        # Too high
        backend = HNConnectorBackend(stories_per_feed=100)
        assert backend.stories_per_feed == 30


class TestPathResolution:
    """Test virtual path resolution."""

    def test_resolve_root(self, hn_connector: HNConnectorBackend) -> None:
        """Test resolving root path."""
        feed, rank = hn_connector._resolve_path("")
        assert feed == ""
        assert rank is None

    def test_resolve_feed_directory(self, hn_connector: HNConnectorBackend) -> None:
        """Test resolving feed directory."""
        feed, rank = hn_connector._resolve_path("top")
        assert feed == "top"
        assert rank is None

    def test_resolve_story_file(self, hn_connector: HNConnectorBackend) -> None:
        """Test resolving story file."""
        feed, rank = hn_connector._resolve_path("top/1.json")
        assert feed == "top"
        assert rank == 1

    def test_resolve_with_hn_prefix(self, hn_connector: HNConnectorBackend) -> None:
        """Test resolving path with hn prefix."""
        feed, rank = hn_connector._resolve_path("hn/top/5.json")
        assert feed == "top"
        assert rank == 5

    def test_resolve_with_leading_slash(self, hn_connector: HNConnectorBackend) -> None:
        """Test resolving path with leading slash."""
        feed, rank = hn_connector._resolve_path("/top/3.json")
        assert feed == "top"
        assert rank == 3

    def test_resolve_all_feeds(self, hn_connector: HNConnectorBackend) -> None:
        """Test all valid feeds are resolved."""
        for feed_name in ["top", "new", "best", "ask", "show", "jobs"]:
            feed, rank = hn_connector._resolve_path(f"{feed_name}/1.json")
            assert feed == feed_name
            assert rank == 1

    def test_resolve_invalid_feed(self, hn_connector: HNConnectorBackend) -> None:
        """Test invalid feed raises error."""
        with pytest.raises(BackendError) as exc_info:
            hn_connector._resolve_path("invalid/1.json")
        assert "Unknown feed" in str(exc_info.value)

    def test_resolve_invalid_rank(self, hn_connector: HNConnectorBackend) -> None:
        """Test invalid rank raises error."""
        with pytest.raises(BackendError) as exc_info:
            hn_connector._resolve_path("top/abc.json")
        assert "Invalid rank" in str(exc_info.value)

    def test_resolve_rank_out_of_range(self, hn_connector: HNConnectorBackend) -> None:
        """Test rank out of range raises error."""
        with pytest.raises(BackendError) as exc_info:
            hn_connector._resolve_path("top/100.json")
        assert "out of range" in str(exc_info.value)


class TestDirectoryOperations:
    """Test directory operations."""

    def test_is_directory_root(self, hn_connector: HNConnectorBackend) -> None:
        """Test root is a directory."""
        assert hn_connector.is_directory("") is True
        assert hn_connector.is_directory("/") is True
        assert hn_connector.is_directory("hn") is True

    def test_is_directory_feed(self, hn_connector: HNConnectorBackend) -> None:
        """Test feed paths are directories."""
        for feed in ["top", "new", "best", "ask", "show", "jobs"]:
            assert hn_connector.is_directory(feed) is True

    def test_is_directory_file(self, hn_connector: HNConnectorBackend) -> None:
        """Test file paths are not directories."""
        assert hn_connector.is_directory("top/1.json") is False

    def test_list_dir_root(self, hn_connector: HNConnectorBackend) -> None:
        """Test listing root directory."""
        entries = hn_connector.list_dir("")
        assert entries == ["top/", "new/", "best/", "ask/", "show/", "jobs/"]

    def test_list_dir_feed(self, hn_connector: HNConnectorBackend) -> None:
        """Test listing feed directory."""
        entries = hn_connector.list_dir("top")
        expected = [f"{i}.json" for i in range(1, 11)]
        assert entries == expected

    def test_list_dir_not_found(self, hn_connector: HNConnectorBackend) -> None:
        """Test listing non-existent directory."""
        with pytest.raises(FileNotFoundError):
            hn_connector.list_dir("nonexistent")


class TestReadOnly:
    """Test read-only behavior."""

    def test_write_not_supported(self, hn_connector: HNConnectorBackend) -> None:
        """Test write raises NotImplementedError."""
        with pytest.raises(NotImplementedError) as exc_info:
            hn_connector.write_content(b"test")
        assert "read-only" in str(exc_info.value)

    def test_delete_not_supported(self, hn_connector: HNConnectorBackend) -> None:
        """Test delete raises NotImplementedError."""
        with pytest.raises(NotImplementedError) as exc_info:
            hn_connector.delete_content("hash")
        assert "read-only" in str(exc_info.value)

    def test_mkdir_not_supported(self, hn_connector: HNConnectorBackend) -> None:
        """Test mkdir raises NotImplementedError."""
        with pytest.raises(NotImplementedError) as exc_info:
            hn_connector.mkdir("/hn/custom")
        assert "fixed virtual structure" in str(exc_info.value)

    def test_rmdir_not_supported(self, hn_connector: HNConnectorBackend) -> None:
        """Test rmdir raises NotImplementedError."""
        with pytest.raises(NotImplementedError) as exc_info:
            hn_connector.rmdir("/hn/top")
        assert "fixed virtual structure" in str(exc_info.value)


class TestContentExists:
    """Test content_exists method."""

    def test_exists_valid_path(self, hn_connector: HNConnectorBackend) -> None:
        """Test valid paths exist."""
        context = OperationContext(user="test", groups=[], backend_path="top/1.json")
        assert hn_connector.content_exists("", context) is True

    def test_exists_directory(self, hn_connector: HNConnectorBackend) -> None:
        """Test directories exist."""
        context = OperationContext(user="test", groups=[], backend_path="top")
        assert hn_connector.content_exists("", context) is True

    def test_exists_invalid_path(self, hn_connector: HNConnectorBackend) -> None:
        """Test invalid paths don't exist."""
        context = OperationContext(user="test", groups=[], backend_path="invalid/1.json")
        assert hn_connector.content_exists("", context) is False

    def test_exists_no_context(self, hn_connector: HNConnectorBackend) -> None:
        """Test no context returns False."""
        assert hn_connector.content_exists("") is False


class TestReadContent:
    """Test read_content method."""

    def test_read_requires_context(self, hn_connector: HNConnectorBackend) -> None:
        """Test read requires context with backend_path."""
        with pytest.raises(BackendError) as exc_info:
            hn_connector.read_content("")
        assert "requires context" in str(exc_info.value)

    def test_read_directory_fails(self, hn_connector: HNConnectorBackend) -> None:
        """Test reading directory fails."""
        context = OperationContext(user="test", groups=[], backend_path="top")
        with pytest.raises(BackendError) as exc_info:
            hn_connector.read_content("", context)
        assert "Cannot read directory" in str(exc_info.value)


class TestAPIFetching:
    """Test HN API fetching methods."""

    @pytest.mark.asyncio
    async def test_fetch_item(
        self, hn_connector: HNConnectorBackend, mock_httpx_client: AsyncMock, sample_story: dict
    ) -> None:
        """Test fetching single item."""
        mock_response = Mock()
        mock_response.json.return_value = sample_story
        mock_response.raise_for_status = Mock()
        mock_httpx_client.get.return_value = mock_response

        item = await hn_connector._fetch_item(12345)

        assert item == sample_story
        mock_httpx_client.get.assert_called_once_with("/item/12345.json")

    @pytest.mark.asyncio
    async def test_fetch_story_ids(
        self, hn_connector: HNConnectorBackend, mock_httpx_client: AsyncMock
    ) -> None:
        """Test fetching story IDs for a feed."""
        story_ids = [12345, 12346, 12347]
        mock_response = Mock()
        mock_response.json.return_value = story_ids
        mock_response.raise_for_status = Mock()
        mock_httpx_client.get.return_value = mock_response

        result = await hn_connector._fetch_story_ids("top")

        assert result == story_ids
        mock_httpx_client.get.assert_called_once_with("/topstories.json")

    @pytest.mark.asyncio
    async def test_fetch_story_ids_all_feeds(
        self, hn_connector: HNConnectorBackend, mock_httpx_client: AsyncMock
    ) -> None:
        """Test fetching story IDs for all feeds."""
        mock_response = Mock()
        mock_response.json.return_value = [1, 2, 3]
        mock_response.raise_for_status = Mock()
        mock_httpx_client.get.return_value = mock_response

        feed_endpoints = {
            "top": "/topstories.json",
            "new": "/newstories.json",
            "best": "/beststories.json",
            "ask": "/askstories.json",
            "show": "/showstories.json",
            "jobs": "/jobstories.json",
        }

        for feed, endpoint in feed_endpoints.items():
            mock_httpx_client.get.reset_mock()
            await hn_connector._fetch_story_ids(feed)
            mock_httpx_client.get.assert_called_once_with(endpoint)

    @pytest.mark.asyncio
    async def test_fetch_story_ids_invalid_feed(
        self, hn_connector: HNConnectorBackend, mock_httpx_client: AsyncMock
    ) -> None:
        """Test fetching story IDs for invalid feed."""
        with pytest.raises(BackendError) as exc_info:
            await hn_connector._fetch_story_ids("invalid")
        assert "Unknown feed" in str(exc_info.value)


class TestRegistration:
    """Test connector registration."""

    def test_registered_in_registry(self) -> None:
        """Test HN connector is registered."""
        from nexus.backends.registry import ConnectorRegistry

        assert ConnectorRegistry.is_registered("hn_connector")

    def test_registry_info(self) -> None:
        """Test registry info is correct."""
        from nexus.backends.registry import ConnectorRegistry

        info = ConnectorRegistry.get_info("hn_connector")
        assert info.name == "hn_connector"
        assert info.category == "api"
        assert "httpx" in info.requires

    def test_connection_args(self) -> None:
        """Test connection args are defined."""
        from nexus.backends.registry import ConnectorRegistry

        args = ConnectorRegistry.get_connection_args("hn_connector")
        assert "cache_ttl" in args
        assert "stories_per_feed" in args
        assert "include_comments" in args
