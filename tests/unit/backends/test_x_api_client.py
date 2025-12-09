"""Unit tests for X (Twitter) API client."""

from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from nexus.backends.x_api_client import RateLimitError, XAPIClient
from nexus.core.exceptions import BackendError


class TestRateLimitError:
    """Test RateLimitError exception."""

    def test_rate_limit_error_creation(self) -> None:
        """Test creating RateLimitError."""
        reset_at = datetime(2025, 1, 1, 12, 0, 0)
        error = RateLimitError(
            endpoint="/users/me",
            reset_at=reset_at,
            wait_seconds=300,
            message="Rate limit exceeded",
        )

        assert error.endpoint == "/users/me"
        assert error.reset_at == reset_at
        assert error.wait_seconds == 300
        assert "Rate limit exceeded" in str(error)
        assert error.backend == "x"


class TestXAPIClientInit:
    """Test XAPIClient initialization."""

    def test_init(self) -> None:
        """Test basic initialization."""
        client = XAPIClient(access_token="test-token")
        assert client.access_token == "test-token"
        assert str(client._client.base_url).rstrip("/") == "https://api.twitter.com/2"
        assert "Authorization" in client._client.headers
        assert client._client.headers["Authorization"] == "Bearer test-token"
        assert client._client.headers["Content-Type"] == "application/json"

    def test_base_url(self) -> None:
        """Test BASE_URL constant."""
        assert XAPIClient.BASE_URL == "https://api.twitter.com/2"


@pytest.mark.asyncio
class TestXAPIClientContextManager:
    """Test async context manager functionality."""

    async def test_context_manager(self) -> None:
        """Test using client as async context manager."""
        async with XAPIClient(access_token="test-token") as client:
            assert isinstance(client, XAPIClient)
            assert client.access_token == "test-token"

    async def test_close(self) -> None:
        """Test closing the client."""
        client = XAPIClient(access_token="test-token")
        with patch.object(client._client, "aclose", new_callable=AsyncMock) as mock_close:
            await client.close()
            mock_close.assert_called_once()


@pytest.mark.asyncio
class TestXAPIClientGetMe:
    """Test get_me() method."""

    async def test_get_me_success(self) -> None:
        """Test getting authenticated user profile."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "id": "123",
                "username": "testuser",
                "name": "Test User",
            }
        }

        client = XAPIClient(access_token="test-token")
        with patch.object(client._client, "request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            result = await client.get_me()

            assert result["data"]["username"] == "testuser"
            mock_request.assert_called_once()
            assert mock_request.call_args[0][0] == "GET"
            assert mock_request.call_args[0][1] == "/users/me"


@pytest.mark.asyncio
class TestXAPIClientGetUserByUsername:
    """Test get_user_by_username() method."""

    async def test_get_user_by_username_success(self) -> None:
        """Test getting user by username."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "id": "456",
                "username": "targetuser",
                "name": "Target User",
            }
        }

        client = XAPIClient(access_token="test-token")
        with patch.object(client._client, "request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            result = await client.get_user_by_username("targetuser")

            assert result["data"]["username"] == "targetuser"
            mock_request.assert_called_once()
            assert "/users/by/username/targetuser" in str(mock_request.call_args)


@pytest.mark.asyncio
class TestXAPIClientGetUserTimeline:
    """Test get_user_timeline() method."""

    async def test_get_user_timeline_basic(self) -> None:
        """Test getting user timeline with basic parameters."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": [{"id": "1", "text": "Tweet 1"}]}

        client = XAPIClient(access_token="test-token")
        with patch.object(client._client, "request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            result = await client.get_user_timeline("123")

            assert len(result["data"]) == 1
            assert result["data"][0]["text"] == "Tweet 1"

    async def test_get_user_timeline_with_all_params(self) -> None:
        """Test getting user timeline with all parameters."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": []}

        client = XAPIClient(access_token="test-token")
        with patch.object(client._client, "request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            await client.get_user_timeline(
                "123",
                max_results=50,
                start_time="2025-01-01T00:00:00Z",
                end_time="2025-01-02T00:00:00Z",
                pagination_token="abc123",
            )

            call_kwargs = mock_request.call_args[1]
            params = call_kwargs["params"]
            assert params["max_results"] == 50
            assert params["start_time"] == "2025-01-01T00:00:00Z"
            assert params["end_time"] == "2025-01-02T00:00:00Z"
            assert params["pagination_token"] == "abc123"

    async def test_get_user_timeline_max_results_capped(self) -> None:
        """Test that max_results is capped at 100."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": []}

        client = XAPIClient(access_token="test-token")
        with patch.object(client._client, "request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            await client.get_user_timeline("123", max_results=200)

            call_kwargs = mock_request.call_args[1]
            assert call_kwargs["params"]["max_results"] == 100


@pytest.mark.asyncio
class TestXAPIClientGetUserTweets:
    """Test get_user_tweets() method."""

    async def test_get_user_tweets_basic(self) -> None:
        """Test getting user tweets."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": [{"id": "1", "text": "My tweet"}]}

        client = XAPIClient(access_token="test-token")
        with patch.object(client._client, "request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            result = await client.get_user_tweets("123")

            assert len(result["data"]) == 1
            assert "/users/123/tweets" in str(mock_request.call_args)


@pytest.mark.asyncio
class TestXAPIClientGetMentions:
    """Test get_mentions() method."""

    async def test_get_mentions_basic(self) -> None:
        """Test getting mentions."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": [{"id": "1", "text": "@testuser hello"}]}

        client = XAPIClient(access_token="test-token")
        with patch.object(client._client, "request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            result = await client.get_mentions("123")

            assert len(result["data"]) == 1
            assert "/users/123/mentions" in str(mock_request.call_args)


@pytest.mark.asyncio
class TestXAPIClientGetTweet:
    """Test get_tweet() method."""

    async def test_get_tweet(self) -> None:
        """Test getting a single tweet."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": {"id": "789", "text": "Single tweet"}}

        client = XAPIClient(access_token="test-token")
        with patch.object(client._client, "request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            result = await client.get_tweet("789")

            assert result["data"]["id"] == "789"
            assert "/tweets/789" in str(mock_request.call_args)


@pytest.mark.asyncio
class TestXAPIClientCreateTweet:
    """Test create_tweet() method."""

    async def test_create_tweet_simple(self) -> None:
        """Test creating a simple tweet."""
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"data": {"id": "999", "text": "Hello World"}}

        client = XAPIClient(access_token="test-token")
        with patch.object(client._client, "request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            result = await client.create_tweet("Hello World")

            assert result["data"]["id"] == "999"
            call_kwargs = mock_request.call_args[1]
            assert call_kwargs["json"]["text"] == "Hello World"

    async def test_create_tweet_with_reply(self) -> None:
        """Test creating a reply tweet."""
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"data": {"id": "999"}}

        client = XAPIClient(access_token="test-token")
        with patch.object(client._client, "request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            await client.create_tweet("Reply text", reply_to="123")

            call_kwargs = mock_request.call_args[1]
            assert call_kwargs["json"]["reply"]["in_reply_to_tweet_id"] == "123"

    async def test_create_tweet_with_quote(self) -> None:
        """Test creating a quote tweet."""
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"data": {"id": "999"}}

        client = XAPIClient(access_token="test-token")
        with patch.object(client._client, "request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            await client.create_tweet("Quote text", quote_tweet_id="456")

            call_kwargs = mock_request.call_args[1]
            assert call_kwargs["json"]["quote_tweet_id"] == "456"

    async def test_create_tweet_with_media(self) -> None:
        """Test creating a tweet with media."""
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"data": {"id": "999"}}

        client = XAPIClient(access_token="test-token")
        with patch.object(client._client, "request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            await client.create_tweet("With media", media_ids=["media1", "media2"])

            call_kwargs = mock_request.call_args[1]
            assert call_kwargs["json"]["media"]["media_ids"] == ["media1", "media2"]

    async def test_create_tweet_with_poll(self) -> None:
        """Test creating a tweet with poll."""
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"data": {"id": "999"}}

        client = XAPIClient(access_token="test-token")
        with patch.object(client._client, "request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            await client.create_tweet(
                "Poll tweet", poll_options=["Option 1", "Option 2"], poll_duration_minutes=60
            )

            call_kwargs = mock_request.call_args[1]
            assert call_kwargs["json"]["poll"]["options"] == ["Option 1", "Option 2"]
            assert call_kwargs["json"]["poll"]["duration_minutes"] == 60

    async def test_create_tweet_poll_default_duration(self) -> None:
        """Test that poll defaults to 24 hours duration."""
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"data": {"id": "999"}}

        client = XAPIClient(access_token="test-token")
        with patch.object(client._client, "request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            await client.create_tweet("Poll tweet", poll_options=["Option 1", "Option 2"])

            call_kwargs = mock_request.call_args[1]
            assert call_kwargs["json"]["poll"]["duration_minutes"] == 1440  # 24 hours

    async def test_create_tweet_poll_invalid_options(self) -> None:
        """Test that poll with invalid number of options raises error."""
        client = XAPIClient(access_token="test-token")

        # Too few options
        with pytest.raises(ValueError, match="Poll must have 2-4 options"):
            await client.create_tweet("Poll tweet", poll_options=["Only one"])

        # Too many options
        with pytest.raises(ValueError, match="Poll must have 2-4 options"):
            await client.create_tweet(
                "Poll tweet",
                poll_options=["Option 1", "Option 2", "Option 3", "Option 4", "Option 5"],
            )


@pytest.mark.asyncio
class TestXAPIClientDeleteTweet:
    """Test delete_tweet() method."""

    async def test_delete_tweet(self) -> None:
        """Test deleting a tweet."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": {"deleted": True}}

        client = XAPIClient(access_token="test-token")
        with patch.object(client._client, "request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            result = await client.delete_tweet("123")

            assert result["data"]["deleted"] is True
            assert mock_request.call_args[0][0] == "DELETE"
            assert "/tweets/123" in str(mock_request.call_args)


@pytest.mark.asyncio
class TestXAPIClientSearchRecentTweets:
    """Test search_recent_tweets() method."""

    async def test_search_recent_tweets_basic(self) -> None:
        """Test basic tweet search."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": [{"id": "1", "text": "Python is great"}]}

        client = XAPIClient(access_token="test-token")
        with patch.object(client._client, "request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            result = await client.search_recent_tweets("python")

            assert len(result["data"]) == 1
            call_kwargs = mock_request.call_args[1]
            assert call_kwargs["params"]["query"] == "python"

    async def test_search_recent_tweets_with_params(self) -> None:
        """Test tweet search with all parameters."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": []}

        client = XAPIClient(access_token="test-token")
        with patch.object(client._client, "request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            await client.search_recent_tweets(
                "python lang:en",
                max_results=50,
                start_time="2025-01-01T00:00:00Z",
                end_time="2025-01-02T00:00:00Z",
                pagination_token="next_token",
            )

            call_kwargs = mock_request.call_args[1]
            params = call_kwargs["params"]
            assert params["query"] == "python lang:en"
            assert params["max_results"] == 50
            assert params["next_token"] == "next_token"


@pytest.mark.asyncio
class TestXAPIClientGetBookmarks:
    """Test get_bookmarks() method."""

    async def test_get_bookmarks_basic(self) -> None:
        """Test getting bookmarks."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": [{"id": "1", "text": "Bookmarked tweet"}]}

        client = XAPIClient(access_token="test-token")
        with patch.object(client._client, "request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            result = await client.get_bookmarks("123")

            assert len(result["data"]) == 1
            assert "/users/123/bookmarks" in str(mock_request.call_args)

    async def test_get_bookmarks_with_pagination(self) -> None:
        """Test getting bookmarks with pagination."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": []}

        client = XAPIClient(access_token="test-token")
        with patch.object(client._client, "request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            await client.get_bookmarks("123", max_results=50, pagination_token="token")

            call_kwargs = mock_request.call_args[1]
            assert call_kwargs["params"]["pagination_token"] == "token"


@pytest.mark.asyncio
class TestXAPIClientRequest:
    """Test _request() method and error handling."""

    async def test_request_rate_limit_error(self) -> None:
        """Test handling rate limit error."""
        mock_response = Mock()
        mock_response.status_code = 429
        mock_response.headers = {"x-rate-limit-reset": str(int(datetime.now().timestamp()) + 900)}

        client = XAPIClient(access_token="test-token")
        with patch.object(client._client, "request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            with pytest.raises(RateLimitError) as exc_info:
                await client._request("GET", "/users/me")

            assert exc_info.value.endpoint == "/users/me"
            assert exc_info.value.wait_seconds >= 0

    async def test_request_http_error(self) -> None:
        """Test handling HTTP error."""
        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.text = "Not found"
        mock_response.json.return_value = {"detail": "User not found"}
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404", request=Mock(), response=mock_response
        )

        client = XAPIClient(access_token="test-token")
        with patch.object(client._client, "request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response

            with pytest.raises(BackendError, match="X API error"):
                await client._request("GET", "/users/invalid")

    async def test_request_generic_error(self) -> None:
        """Test handling generic request error."""
        client = XAPIClient(access_token="test-token")
        with patch.object(client._client, "request", new_callable=AsyncMock) as mock_request:
            mock_request.side_effect = Exception("Network error")

            with pytest.raises(BackendError, match="X API request failed"):
                await client._request("GET", "/users/me")

    async def test_request_success_with_debug_logging(self) -> None:
        """Test successful request with debug logging."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.headers = {
            "x-rate-limit-remaining": "100",
            "x-rate-limit-limit": "150",
            "x-rate-limit-reset": "1735689600",
        }
        mock_response.json.return_value = {"data": {"id": "123"}}

        client = XAPIClient(access_token="test-token")
        with (
            patch.object(client._client, "request", new_callable=AsyncMock) as mock_request,
            patch("nexus.backends.x_api_client.logger") as mock_logger,
        ):
            mock_request.return_value = mock_response
            mock_logger.isEnabledFor.return_value = True

            result = await client._request("GET", "/users/me")

            assert result["data"]["id"] == "123"
            mock_logger.debug.assert_called_once()
