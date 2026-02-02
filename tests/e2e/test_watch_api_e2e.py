"""E2E tests for the /api/watch long-polling endpoint (Issue #1117).

Tests the REST API endpoint for watching file system changes.
Uses the shared nexus_fs fixture from conftest.py.

Note: Some tests require event infrastructure (Redis or same-box backend).
Tests gracefully handle the 501 response when events are not available.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

if TYPE_CHECKING:
    from nexus import NexusFS


# Note: nexus_fs fixture is provided by conftest.py


class TestWatchAPIValidation:
    """Tests for parameter validation (don't require event infrastructure)."""

    def test_watch_invalid_timeout_too_high(self, nexus_fs: NexusFS) -> None:
        """Test that timeout > 300 is rejected."""
        from nexus.server.fastapi_server import create_app

        app = create_app(nexus_fs)

        with TestClient(app) as client:
            response = client.get("/api/watch", params={"path": "/inbox/", "timeout": 500})

            assert response.status_code == 422  # Validation error

    def test_watch_invalid_timeout_too_low(self, nexus_fs: NexusFS) -> None:
        """Test that timeout < 0.1 is rejected."""
        from nexus.server.fastapi_server import create_app

        app = create_app(nexus_fs)

        with TestClient(app) as client:
            response = client.get("/api/watch", params={"path": "/inbox/", "timeout": 0.01})

            assert response.status_code == 422  # Validation error


class TestWatchAPIEndpoint:
    """Tests for GET /api/watch endpoint (may return 501 without event infrastructure)."""

    def test_watch_returns_valid_response(self, nexus_fs: NexusFS) -> None:
        """Test that watch returns a valid response (200 or 501)."""
        from nexus.server.fastapi_server import create_app

        nexus_fs.mkdir("/inbox")
        app = create_app(nexus_fs)

        with TestClient(app) as client:
            response = client.get("/api/watch", params={"path": "/inbox/", "timeout": 0.1})

            # Either success with timeout, or 501 if no event source
            assert response.status_code in (200, 501)

            if response.status_code == 200:
                data = response.json()
                assert data["timeout"] is True
                assert data["changes"] == []

    def test_watch_default_parameters(self, nexus_fs: NexusFS) -> None:
        """Test watch with default parameters."""
        from nexus.server.fastapi_server import create_app

        app = create_app(nexus_fs)

        with TestClient(app) as client:
            response = client.get("/api/watch", params={"timeout": 0.1})

            # Either success or 501 if no event source
            assert response.status_code in (200, 501)

            if response.status_code == 200:
                data = response.json()
                assert "timeout" in data
                assert "changes" in data

    def test_watch_with_glob_pattern(self, nexus_fs: NexusFS) -> None:
        """Test watch with glob pattern."""
        from nexus.server.fastapi_server import create_app

        nexus_fs.mkdir("/inbox")
        app = create_app(nexus_fs)

        with TestClient(app) as client:
            response = client.get("/api/watch", params={"path": "/inbox/**/*.txt", "timeout": 0.1})

            # Either success or 501 if no event source
            assert response.status_code in (200, 501)

            if response.status_code == 200:
                data = response.json()
                assert data["timeout"] is True

    def test_watch_response_format(self, nexus_fs: NexusFS) -> None:
        """Test that response has correct format when events are available."""
        from nexus.server.fastapi_server import create_app

        nexus_fs.mkdir("/inbox")
        app = create_app(nexus_fs)

        with TestClient(app) as client:
            response = client.get("/api/watch", params={"path": "/inbox/", "timeout": 0.1})

            if response.status_code == 200:
                data = response.json()

                # Verify response structure
                assert isinstance(data, dict)
                assert "changes" in data
                assert "timeout" in data
                assert isinstance(data["changes"], list)
                assert isinstance(data["timeout"], bool)
            else:
                # 501 Not Implemented is acceptable without event infrastructure
                assert response.status_code == 501
                assert "Watch not available" in response.json()["detail"]


class TestWatchAPIWithEvents:
    """Tests for watch endpoint detecting actual changes.

    These tests require event infrastructure (Redis or same-box backend).
    """

    @pytest.mark.asyncio
    async def test_watch_detects_file_write(self, nexus_fs: NexusFS) -> None:
        """Test that watch detects file write events (requires event infrastructure)."""
        from nexus.server.fastapi_server import create_app

        nexus_fs.mkdir("/inbox")
        app = create_app(nexus_fs)

        with TestClient(app) as client:
            # First check if events are available
            check_response = client.get("/api/watch", params={"path": "/inbox/", "timeout": 0.1})

            if check_response.status_code == 501:
                pytest.skip("Event infrastructure not available")

            # Start watch in background thread
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(
                    client.get,
                    "/api/watch",
                    params={"path": "/inbox/", "timeout": 5.0},
                )

                # Give watch time to start
                await asyncio.sleep(0.2)

                # Write a file to trigger the event
                nexus_fs.write("/inbox/test.txt", b"hello world")

                # Wait for watch to return
                response = future.result(timeout=6.0)

                assert response.status_code == 200
                data = response.json()
                assert "changes" in data
                assert "timeout" in data


class TestWatchAPIErrorHandling:
    """Tests for error handling in watch endpoint."""

    def test_watch_service_unavailable(self) -> None:
        """Test 503 when NexusFS is not initialized."""
        from nexus.server.fastapi_server import create_app

        # Create app without NexusFS
        app = create_app(None)  # type: ignore[arg-type]

        with TestClient(app) as client:
            response = client.get("/api/watch", params={"path": "/test/", "timeout": 0.1})

            assert response.status_code == 503
            assert "not initialized" in response.json()["detail"].lower()

    def test_watch_not_implemented_message(self, nexus_fs: NexusFS) -> None:
        """Test that 501 response has helpful message."""
        from nexus.server.fastapi_server import create_app

        app = create_app(nexus_fs)

        with TestClient(app) as client:
            response = client.get("/api/watch", params={"timeout": 0.1})

            if response.status_code == 501:
                detail = response.json()["detail"]
                # Should mention what's needed
                assert "Redis" in detail or "event bus" in detail or "same-box" in detail


class TestWatchAPIDocumentation:
    """Tests to verify API documentation is correct."""

    def test_openapi_schema_includes_watch(self, nexus_fs: NexusFS) -> None:
        """Test that /api/watch is documented in OpenAPI schema."""
        from nexus.server.fastapi_server import create_app

        app = create_app(nexus_fs)

        with TestClient(app) as client:
            response = client.get("/openapi.json")

            assert response.status_code == 200
            schema = response.json()

            # Check that /api/watch is in the paths
            assert "/api/watch" in schema["paths"]

            # Check that it has GET method
            watch_path = schema["paths"]["/api/watch"]
            assert "get" in watch_path

            # Check parameters are documented
            get_spec = watch_path["get"]
            assert "parameters" in get_spec

            param_names = [p["name"] for p in get_spec["parameters"]]
            assert "path" in param_names
            assert "timeout" in param_names
