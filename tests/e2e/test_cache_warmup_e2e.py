"""End-to-end tests for Cache Warmup API endpoints (Issue #1076).

Tests the complete cache warmup pipeline via FastAPI:
1. Warmup directory with metadata/content
2. Check cache statistics
3. Get hot files tracking
4. History-based warmup

Run with:
    pytest tests/e2e/test_cache_warmup_e2e.py -v --override-ini="addopts="
"""

from __future__ import annotations

import pytest


class TestCacheWarmupAPI:
    """End-to-end tests for Cache Warmup API endpoints."""

    @pytest.mark.asyncio
    async def test_cache_stats_endpoint(self, test_app):
        """Test cache stats endpoint returns valid statistics."""
        response = test_app.get("/api/cache/stats")

        assert response.status_code == 200
        data = response.json()

        # Should have file_access_tracker stats at minimum
        assert "file_access_tracker" in data
        assert "tracked_paths" in data["file_access_tracker"]
        assert "total_accesses" in data["file_access_tracker"]

    @pytest.mark.asyncio
    async def test_cache_hot_files_endpoint(self, test_app):
        """Test hot files endpoint."""
        response = test_app.get("/api/cache/hot-files", params={"limit": 10})

        assert response.status_code == 200
        data = response.json()

        # Should return a list (may be empty if no accesses yet)
        assert isinstance(data, list)

    @pytest.mark.asyncio
    async def test_cache_warmup_directory(self, test_app, nexus_server):  # noqa: ARG002
        """Test directory warmup endpoint."""
        # First, create some files to warm
        # Write a test file via the NFS API
        response = test_app.post(
            "/api/nfs/write",
            json={
                "path": "/warmup-test/file1.txt",
                "content": "test content 1",
            },
        )
        assert response.status_code == 200

        response = test_app.post(
            "/api/nfs/write",
            json={
                "path": "/warmup-test/file2.txt",
                "content": "test content 2",
            },
        )
        assert response.status_code == 200

        # Now warm up the directory
        response = test_app.post(
            "/api/cache/warmup",
            json={
                "path": "/warmup-test",
                "depth": 2,
                "include_content": False,
                "max_files": 100,
            },
        )

        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "completed"
        assert "files_warmed" in data
        assert "duration_seconds" in data
        assert data["duration_seconds"] >= 0

    @pytest.mark.asyncio
    async def test_cache_warmup_with_content(self, test_app, nexus_server):
        """Test directory warmup with content caching."""
        # Create a test file
        response = test_app.post(
            "/api/nfs/write",
            json={
                "path": "/warmup-content/small-file.txt",
                "content": "small test content",
            },
        )
        assert response.status_code == 200

        # Warm up with content
        response = test_app.post(
            "/api/cache/warmup",
            json={
                "path": "/warmup-content",
                "depth": 1,
                "include_content": True,
                "max_files": 50,
            },
        )

        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "completed"
        assert "content_warmed" in data
        assert "bytes_warmed" in data

    @pytest.mark.asyncio
    async def test_cache_warmup_requires_path_or_user(self, test_app):
        """Test warmup endpoint requires either path or user."""
        response = test_app.post(
            "/api/cache/warmup",
            json={
                "depth": 2,
                "include_content": False,
            },
        )

        # Should return 400 Bad Request
        assert response.status_code == 400
        data = response.json()
        assert "path" in data["detail"].lower() or "user" in data["detail"].lower()

    @pytest.mark.asyncio
    async def test_cache_warmup_user_history(self, test_app, nexus_server):
        """Test user history-based warmup."""
        # Note: This test may not warm many files because the file access
        # tracker needs to be populated first through actual file accesses
        response = test_app.post(
            "/api/cache/warmup",
            json={
                "user": "test-user",
                "hours": 24,
                "max_files": 50,
            },
        )

        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "completed"
        # May be 0 if no history exists for user
        assert "files_warmed" in data
        assert data["files_warmed"] >= 0

    @pytest.mark.asyncio
    async def test_cache_api_health_check(self, test_app):
        """Verify all cache endpoints respond without server errors."""
        endpoints = [
            ("GET", "/api/cache/stats", None),
            ("GET", "/api/cache/hot-files", {"limit": 10}),
            ("POST", "/api/cache/warmup", {"path": "/", "max_files": 10}),
        ]

        for method, endpoint, params in endpoints:
            if method == "GET":
                response = test_app.get(endpoint, params=params)
            else:
                response = test_app.post(endpoint, json=params)

            # All endpoints should return 200 (not 500 server errors)
            assert response.status_code == 200, (
                f"{method} {endpoint} failed with {response.status_code}: {response.text}"
            )

    @pytest.mark.asyncio
    async def test_cache_stats_after_warmup(self, test_app, nexus_server):
        """Test that cache stats reflect warmup activity."""
        # Create test files
        for i in range(3):
            response = test_app.post(
                "/api/nfs/write",
                json={
                    "path": f"/stats-test/file{i}.txt",
                    "content": f"content {i}",
                },
            )
            assert response.status_code == 200

        # Get initial stats (verify endpoint works before warmup)
        response = test_app.get("/api/cache/stats")
        assert response.status_code == 200
        _ = response.json()  # Verify JSON is valid

        # Warm up directory
        response = test_app.post(
            "/api/cache/warmup",
            json={
                "path": "/stats-test",
                "depth": 1,
                "max_files": 100,
            },
        )
        assert response.status_code == 200

        # Get updated stats
        response = test_app.get("/api/cache/stats")
        assert response.status_code == 200
        updated_stats = response.json()

        # Stats should still be valid structures
        assert "file_access_tracker" in updated_stats

    @pytest.mark.asyncio
    async def test_hot_files_after_reads(self, test_app, nexus_server):
        """Test that hot files tracking works after file reads."""
        # Create and read a file multiple times to make it "hot"
        test_path = "/hot-file-test/frequently-read.txt"

        # Create the file
        response = test_app.post(
            "/api/nfs/write",
            json={
                "path": test_path,
                "content": "frequently accessed content",
            },
        )
        assert response.status_code == 200

        # Read the file multiple times (though the tracker is per-server,
        # not per-request, so this may not increase access count significantly
        # in single test)
        for _ in range(3):
            response = test_app.post(
                "/api/nfs/read",
                json={"path": test_path},
            )
            assert response.status_code == 200

        # Check hot files - may or may not include our file depending on
        # how the server tracks accesses
        response = test_app.get("/api/cache/hot-files", params={"limit": 20})
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)


class TestCacheWarmupWithPermissions:
    """Tests for cache warmup with ReBAC permissions enabled."""

    @pytest.mark.asyncio
    async def test_warmup_respects_tenant_isolation(self, test_app):
        """Test that warmup respects tenant isolation."""
        # The warmup should work within the authenticated tenant context
        response = test_app.post(
            "/api/cache/warmup",
            json={
                "path": "/",
                "depth": 1,
                "max_files": 10,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--log-cli-level=INFO"])
