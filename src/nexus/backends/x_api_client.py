"""X (Twitter) API v2 client wrapper.

Provides a simple interface for X API v2 endpoints with automatic
rate limit handling and response transformation.
"""

import logging
from datetime import datetime
from typing import Any

import httpx

from nexus.core.exceptions import BackendError

logger = logging.getLogger(__name__)


class RateLimitError(BackendError):
    """Rate limit exceeded error."""

    def __init__(
        self,
        endpoint: str,
        reset_at: datetime,
        wait_seconds: float,
        message: str,
    ):
        super().__init__(message, backend="x")
        self.endpoint = endpoint
        self.reset_at = reset_at
        self.wait_seconds = wait_seconds


class XAPIClient:
    """X (Twitter) API v2 client.

    Provides methods for common X API operations:
    - Get user timeline
    - Get mentions
    - Get user tweets
    - Post tweets
    - Delete tweets
    - Search tweets
    - Get user profiles
    """

    BASE_URL = "https://api.twitter.com/2"

    def __init__(self, access_token: str):
        """Initialize X API client.

        Args:
            access_token: OAuth 2.0 access token
        """
        self.access_token = access_token
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    async def close(self) -> None:
        """Close HTTP client."""
        await self._client.aclose()

    async def __aenter__(self) -> "XAPIClient":
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: object
    ) -> None:
        await self.close()

    # === User/Authentication ===

    async def get_me(self) -> dict[str, Any]:
        """Get authenticated user's profile.

        Returns:
            User profile data

        Example:
            >>> client = XAPIClient(token)
            >>> user = await client.get_me()
            >>> print(user["data"]["username"])
        """
        params = {
            "user.fields": "created_at,description,id,name,username,verified,profile_image_url,public_metrics"
        }
        return await self._request("GET", "/users/me", params=params)

    async def get_user_by_username(self, username: str) -> dict[str, Any]:
        """Get user profile by username.

        Args:
            username: X username (without @)

        Returns:
            User profile data
        """
        params = {
            "user.fields": "created_at,description,id,name,username,verified,profile_image_url,public_metrics"
        }
        return await self._request("GET", f"/users/by/username/{username}", params=params)

    # === Tweets ===

    async def get_user_timeline(
        self,
        user_id: str,
        max_results: int = 100,
        start_time: str | None = None,
        end_time: str | None = None,
        pagination_token: str | None = None,
    ) -> dict[str, Any]:
        """Get user's home timeline (reverse chronological).

        Args:
            user_id: X user ID
            max_results: Number of tweets to fetch (5-100, default 100)
            start_time: Start time (ISO 8601 format)
            end_time: End time (ISO 8601 format)
            pagination_token: Pagination token for next page

        Returns:
            Timeline response with tweets data
        """
        params: dict[str, Any] = {
            "max_results": min(max_results, 100),
            "tweet.fields": "created_at,author_id,text,attachments,public_metrics,referenced_tweets,entities",
            "expansions": "author_id,attachments.media_keys,referenced_tweets.id",
            "media.fields": "url,preview_image_url,type,width,height,duration_ms",
            "user.fields": "username,name,profile_image_url,verified",
        }

        if start_time:
            params["start_time"] = start_time
        if end_time:
            params["end_time"] = end_time
        if pagination_token:
            params["pagination_token"] = pagination_token

        return await self._request(
            "GET",
            f"/users/{user_id}/timelines/reverse_chronological",
            params=params,
        )

    async def get_user_tweets(
        self,
        user_id: str,
        max_results: int = 100,
        start_time: str | None = None,
        end_time: str | None = None,
        pagination_token: str | None = None,
    ) -> dict[str, Any]:
        """Get user's tweets (including retweets, replies).

        Args:
            user_id: X user ID
            max_results: Number of tweets to fetch (5-100, default 100)
            start_time: Start time (ISO 8601 format)
            end_time: End time (ISO 8601 format)
            pagination_token: Pagination token for next page

        Returns:
            Tweets response with data
        """
        params: dict[str, Any] = {
            "max_results": min(max_results, 100),
            "tweet.fields": "created_at,author_id,text,attachments,public_metrics,referenced_tweets,entities",
            "expansions": "attachments.media_keys,referenced_tweets.id",
            "media.fields": "url,preview_image_url,type,width,height,duration_ms",
        }

        if start_time:
            params["start_time"] = start_time
        if end_time:
            params["end_time"] = end_time
        if pagination_token:
            params["pagination_token"] = pagination_token

        return await self._request("GET", f"/users/{user_id}/tweets", params=params)

    async def get_mentions(
        self,
        user_id: str,
        max_results: int = 100,
        start_time: str | None = None,
        end_time: str | None = None,
        pagination_token: str | None = None,
    ) -> dict[str, Any]:
        """Get mentions for user.

        Args:
            user_id: X user ID
            max_results: Number of mentions to fetch (5-100, default 100)
            start_time: Start time (ISO 8601 format)
            end_time: End time (ISO 8601 format)
            pagination_token: Pagination token for next page

        Returns:
            Mentions response with data
        """
        params: dict[str, Any] = {
            "max_results": min(max_results, 100),
            "tweet.fields": "created_at,author_id,text,attachments,public_metrics,referenced_tweets",
            "expansions": "author_id,attachments.media_keys",
            "media.fields": "url,preview_image_url,type,width,height",
            "user.fields": "username,name,profile_image_url,verified",
        }

        if start_time:
            params["start_time"] = start_time
        if end_time:
            params["end_time"] = end_time
        if pagination_token:
            params["pagination_token"] = pagination_token

        return await self._request("GET", f"/users/{user_id}/mentions", params=params)

    async def get_tweet(self, tweet_id: str) -> dict[str, Any]:
        """Get single tweet by ID.

        Args:
            tweet_id: Tweet ID

        Returns:
            Tweet data
        """
        params = {
            "tweet.fields": "created_at,author_id,text,attachments,public_metrics,referenced_tweets,entities",
            "expansions": "author_id,attachments.media_keys",
            "media.fields": "url,preview_image_url,type,width,height",
            "user.fields": "username,name,profile_image_url,verified",
        }

        return await self._request("GET", f"/tweets/{tweet_id}", params=params)

    async def create_tweet(
        self,
        text: str,
        reply_to: str | None = None,
        quote_tweet_id: str | None = None,
        media_ids: list[str] | None = None,
        poll_options: list[str] | None = None,
        poll_duration_minutes: int | None = None,
    ) -> dict[str, Any]:
        """Create (post) a new tweet.

        Args:
            text: Tweet text (max 280 characters)
            reply_to: Tweet ID to reply to (optional)
            quote_tweet_id: Tweet ID to quote (optional)
            media_ids: List of media IDs (optional)
            poll_options: Poll options (optional, 2-4 options)
            poll_duration_minutes: Poll duration in minutes (optional)

        Returns:
            Created tweet data

        Example:
            >>> tweet = await client.create_tweet("Hello from Nexus!")
            >>> print(tweet["data"]["id"])
        """
        data: dict[str, Any] = {"text": text}

        # Add reply settings
        if reply_to:
            data["reply"] = {"in_reply_to_tweet_id": reply_to}

        # Add quote tweet
        if quote_tweet_id:
            data["quote_tweet_id"] = quote_tweet_id

        # Add media
        if media_ids:
            data["media"] = {"media_ids": media_ids}

        # Add poll
        if poll_options:
            if len(poll_options) < 2 or len(poll_options) > 4:
                raise ValueError("Poll must have 2-4 options")
            data["poll"] = {
                "options": poll_options,
                "duration_minutes": poll_duration_minutes or 1440,  # Default 24 hours
            }

        return await self._request("POST", "/tweets", json=data)

    async def delete_tweet(self, tweet_id: str) -> dict[str, Any]:
        """Delete a tweet.

        Args:
            tweet_id: Tweet ID to delete

        Returns:
            Deletion response

        Example:
            >>> result = await client.delete_tweet("1234567890")
        """
        return await self._request("DELETE", f"/tweets/{tweet_id}")

    # === Search ===

    async def search_recent_tweets(
        self,
        query: str,
        max_results: int = 100,
        start_time: str | None = None,
        end_time: str | None = None,
        pagination_token: str | None = None,
    ) -> dict[str, Any]:
        """Search recent tweets (last 7 days).

        Args:
            query: Search query (X search operators)
            max_results: Number of results (10-100, default 100)
            start_time: Start time (ISO 8601 format)
            end_time: End time (ISO 8601 format)
            pagination_token: Pagination token for next page

        Returns:
            Search results

        Example:
            >>> results = await client.search_recent_tweets("python lang:en -is:retweet")
        """
        params: dict[str, Any] = {
            "query": query,
            "max_results": min(max_results, 100),
            "tweet.fields": "created_at,author_id,text,attachments,public_metrics",
            "expansions": "author_id,attachments.media_keys",
            "media.fields": "url,preview_image_url,type",
            "user.fields": "username,name,verified",
        }

        if start_time:
            params["start_time"] = start_time
        if end_time:
            params["end_time"] = end_time
        if pagination_token:
            params["next_token"] = pagination_token

        return await self._request("GET", "/tweets/search/recent", params=params)

    # === Bookmarks ===

    async def get_bookmarks(
        self,
        user_id: str,
        max_results: int = 100,
        pagination_token: str | None = None,
    ) -> dict[str, Any]:
        """Get user's bookmarked tweets.

        Args:
            user_id: X user ID
            max_results: Number of bookmarks (1-100, default 100)
            pagination_token: Pagination token for next page

        Returns:
            Bookmarked tweets
        """
        params: dict[str, Any] = {
            "max_results": min(max_results, 100),
            "tweet.fields": "created_at,author_id,text,attachments,public_metrics",
            "expansions": "author_id,attachments.media_keys",
            "media.fields": "url,preview_image_url,type",
            "user.fields": "username,name,verified",
        }

        if pagination_token:
            params["pagination_token"] = pagination_token

        return await self._request("GET", f"/users/{user_id}/bookmarks", params=params)

    # === Internal ===

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make HTTP request to X API.

        Args:
            method: HTTP method (GET, POST, DELETE, etc.)
            endpoint: API endpoint (e.g., "/users/me")
            params: Query parameters
            json: JSON body

        Returns:
            API response as dict

        Raises:
            RateLimitError: If rate limit exceeded
            BackendError: If API request fails
        """
        try:
            response = await self._client.request(
                method,
                endpoint,
                params=params,
                json=json,
            )

            # Check for rate limit
            if response.status_code == 429:
                reset_time = int(response.headers.get("x-rate-limit-reset", 0))
                reset_at = datetime.fromtimestamp(reset_time) if reset_time else datetime.now()
                wait_seconds = reset_time - datetime.now().timestamp() if reset_time else 900

                raise RateLimitError(
                    endpoint=endpoint,
                    reset_at=reset_at,
                    wait_seconds=max(0, wait_seconds),
                    message=f"Rate limit exceeded for {endpoint}. Resets at {reset_at}",
                )

            # Raise for other HTTP errors
            response.raise_for_status()

            # Log rate limit headers for debugging
            if logger.isEnabledFor(logging.DEBUG):
                remaining = response.headers.get("x-rate-limit-remaining")
                limit = response.headers.get("x-rate-limit-limit")
                reset = response.headers.get("x-rate-limit-reset")
                logger.debug(
                    f"[X-API] {method} {endpoint}: "
                    f"{remaining}/{limit} requests remaining, "
                    f"resets at {reset}"
                )

            result: dict[str, Any] = response.json()
            return result

        except httpx.HTTPStatusError as e:
            # Parse error response
            try:
                error_data = e.response.json()
                error_msg = error_data.get("detail", e.response.text)
            except Exception:
                error_msg = e.response.text

            raise BackendError(
                f"X API error ({e.response.status_code}): {error_msg}",
                backend="x",
                path=endpoint,
            ) from e

        except RateLimitError:
            # Re-raise rate limit errors
            raise

        except Exception as e:
            raise BackendError(
                f"X API request failed: {e}",
                backend="x",
                path=endpoint,
            ) from e
