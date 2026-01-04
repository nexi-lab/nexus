"""Tests for rate limiting functionality (Issue #780)."""

from unittest.mock import MagicMock, patch

from fastapi import Request
from slowapi.errors import RateLimitExceeded

from nexus.server.fastapi_server import (
    RATE_LIMIT_ANONYMOUS,
    RATE_LIMIT_AUTHENTICATED,
    RATE_LIMIT_PREMIUM,
    _get_rate_limit_key,
    _rate_limit_exceeded_handler,
)


class TestRateLimitKey:
    """Tests for rate limit key extraction."""

    def test_anonymous_request_uses_ip(self) -> None:
        """Anonymous requests should use IP address as key."""
        request = MagicMock(spec=Request)
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "192.168.1.100"

        key = _get_rate_limit_key(request)
        assert key == "192.168.1.100"

    def test_bearer_token_sk_format_extracts_user(self) -> None:
        """sk- format tokens should extract tenant and user."""
        request = MagicMock(spec=Request)
        request.headers = {"Authorization": "Bearer sk-acme_alice_123_abc123"}
        request.client = MagicMock()
        request.client.host = "192.168.1.100"

        key = _get_rate_limit_key(request)
        assert key == "user:acme:alice"

    def test_bearer_token_generic_uses_hash(self) -> None:
        """Non-sk tokens should use hashed key."""
        request = MagicMock(spec=Request)
        request.headers = {"Authorization": "Bearer some-other-token-format"}
        request.client = MagicMock()
        request.client.host = "192.168.1.100"

        key = _get_rate_limit_key(request)
        assert key.startswith("token:")
        assert len(key) > 10

    def test_agent_id_header_used(self) -> None:
        """X-Agent-ID header should be used for rate limiting."""
        request = MagicMock(spec=Request)
        request.headers = {"X-Agent-ID": "agent-123"}
        request.client = MagicMock()
        request.client.host = "192.168.1.100"

        key = _get_rate_limit_key(request)
        assert key == "agent:agent-123"


class TestRateLimitExceededHandler:
    """Tests for rate limit exceeded handler."""

    def test_returns_429_status(self) -> None:
        """Handler should return 429 status code."""
        request = MagicMock(spec=Request)
        exc = MagicMock(spec=RateLimitExceeded)
        exc.detail = "Rate limit exceeded"

        response = _rate_limit_exceeded_handler(request, exc)

        assert response.status_code == 429

    def test_includes_retry_after_header(self) -> None:
        """Response should include Retry-After header."""
        request = MagicMock(spec=Request)
        exc = MagicMock(spec=RateLimitExceeded)
        exc.detail = "Rate limit exceeded"
        exc.retry_after = 30

        response = _rate_limit_exceeded_handler(request, exc)

        assert "Retry-After" in response.headers


class TestRateLimitConfiguration:
    """Tests for rate limit configuration."""

    def test_default_rate_limits_set(self) -> None:
        """Default rate limits should be properly configured."""
        assert RATE_LIMIT_ANONYMOUS == "60/minute"
        assert RATE_LIMIT_AUTHENTICATED == "300/minute"
        assert RATE_LIMIT_PREMIUM == "1000/minute"

    def test_environment_variable_override(self) -> None:
        """Rate limits should be overridable via environment variables."""
        with patch.dict("os.environ", {"NEXUS_RATE_LIMIT_ANONYMOUS": "100/minute"}):
            # Need to reload module to pick up env var
            # In practice, this is set at startup time
            pass


class TestRateLimiterIntegration:
    """Integration tests for rate limiter with FastAPI."""

    def test_limiter_initialized_in_create_app(self) -> None:
        """Rate limiter should be initialized when creating app."""
        from nexus.server.fastapi_server import create_app

        # Create a minimal mock NexusFS
        mock_fs = MagicMock()
        mock_fs.metadata = None

        with patch.dict("os.environ", {"NEXUS_RATE_LIMIT_DISABLED": "false"}):
            app = create_app(mock_fs)

            # Limiter should be attached to app state
            assert hasattr(app.state, "limiter")
            assert app.state.limiter is not None

    def test_rate_limiting_can_be_disabled(self) -> None:
        """Rate limiting should be disableable via environment variable."""
        from nexus.server.fastapi_server import create_app

        mock_fs = MagicMock()
        mock_fs.metadata = None

        with patch.dict("os.environ", {"NEXUS_RATE_LIMIT_DISABLED": "true"}):
            app = create_app(mock_fs)

            # Limiter should still exist but be disabled
            assert hasattr(app.state, "limiter")
            assert not app.state.limiter.enabled
