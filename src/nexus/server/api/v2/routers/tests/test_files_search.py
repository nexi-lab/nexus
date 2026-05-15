"""Tests for GET /glob and GET /grep file search endpoints."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.v2.routers.async_files import create_async_files_router
from nexus.server.dependencies import get_auth_result


@pytest.fixture()
def mock_fs() -> MagicMock:
    """Create a mock NexusFS with sys_readdir returning file paths."""
    fs = MagicMock()
    fs.sys_readdir.return_value = [
        "/src/main.py",
        "/src/utils.py",
        "/src/tests/test_main.py",
        "/README.md",
        "/docs/guide.md",
    ]

    # For grep fallback: fs.read returns bytes
    def _read(path: str, **_kwargs: object) -> bytes:
        contents = {
            "/src/main.py": b"import os\ndef main():\n    print('hello')\n",
            "/src/utils.py": b"import sys\ndef helper():\n    return 42\n",
            "/src/tests/test_main.py": b"def test_main():\n    assert True\n",
            "/README.md": b"# Project\nThis is a readme\n",
            "/docs/guide.md": b"# Guide\nSome documentation\n",
        }
        return contents.get(path, b"")

    fs.read.side_effect = _read
    return fs


@pytest.fixture()
def client(mock_fs: MagicMock) -> TestClient:
    """Create a TestClient with mock FS and bypassed auth."""
    app = FastAPI()
    router = create_async_files_router(nexus_fs=mock_fs)
    app.include_router(router)

    # Override auth to return an authenticated result (bypasses real auth)
    app.dependency_overrides[get_auth_result] = lambda: {
        "authenticated": True,
        "user_id": "test-user",
        "groups": [],
        "zone_id": "root",
        "is_admin": False,
    }

    return TestClient(app)


# =============================================================================
# Glob Endpoint Tests
# =============================================================================


class TestGlobEndpoint:
    """Tests for GET /glob."""

    def test_glob_missing_pattern_returns_422(self, client: TestClient) -> None:
        """Missing required 'pattern' query param returns 422 (FastAPI validation)."""
        resp = client.get("/glob")
        assert resp.status_code == 422

    @patch("nexus.server.api.v2.routers.async_files.glob_filter")
    def test_glob_happy_path(self, mock_glob_filter: MagicMock, client: TestClient) -> None:
        """Glob returns matched files with correct structure."""
        mock_glob_filter.return_value = ["/src/main.py", "/src/utils.py"]

        resp = client.get("/glob", params={"pattern": "**/*.py"})
        assert resp.status_code == 200

        data = resp.json()
        assert data["matches"] == ["/src/main.py", "/src/utils.py"]
        assert data["total"] == 2
        assert data["truncated"] is False
        assert data["pattern"] == "**/*.py"
        assert data["base_path"] == "/"

    @patch("nexus.server.api.v2.routers.async_files.glob_filter")
    def test_glob_with_custom_base_path(
        self, mock_glob_filter: MagicMock, client: TestClient, mock_fs: MagicMock
    ) -> None:
        """Glob passes base path to sys_readdir."""
        mock_glob_filter.return_value = []

        resp = client.get("/glob", params={"pattern": "*.py", "path": "/src"})
        assert resp.status_code == 200

        # Verify sys_readdir was called with the custom path
        mock_fs.sys_readdir.assert_called_once()
        call_args = mock_fs.sys_readdir.call_args
        assert (
            call_args[0][0] == "/src"
            or call_args[1].get("path") == "/src"
            or call_args[0] == ("/src",)
        )

    @patch("nexus.server.api.v2.routers.async_files.glob_filter")
    def test_glob_truncation(self, mock_glob_filter: MagicMock, client: TestClient) -> None:
        """When results exceed limit, truncated=True and only limit items returned."""
        mock_glob_filter.return_value = [f"/file{i}.py" for i in range(50)]

        resp = client.get("/glob", params={"pattern": "*.py", "limit": 10})
        assert resp.status_code == 200

        data = resp.json()
        assert len(data["matches"]) == 10
        assert data["total"] == 50
        assert data["truncated"] is True

    @patch("nexus.server.api.v2.routers.async_files.glob_filter")
    def test_glob_no_matches(self, mock_glob_filter: MagicMock, client: TestClient) -> None:
        """Glob with no matching files returns empty list."""
        mock_glob_filter.return_value = []

        resp = client.get("/glob", params={"pattern": "*.xyz"})
        assert resp.status_code == 200

        data = resp.json()
        assert data["matches"] == []
        assert data["total"] == 0
        assert data["truncated"] is False

    def test_glob_limit_max_validation(self, client: TestClient) -> None:
        """Limit exceeding 1000 returns 422 (FastAPI validation)."""
        resp = client.get("/glob", params={"pattern": "*.py", "limit": 2000})
        assert resp.status_code == 422

    def test_glob_limit_min_validation(self, client: TestClient) -> None:
        """Limit below 1 returns 422 (FastAPI validation)."""
        resp = client.get("/glob", params={"pattern": "*.py", "limit": 0})
        assert resp.status_code == 422

    def test_glob_permission_error(self, client: TestClient, mock_fs: MagicMock) -> None:
        """Permission error on readdir returns 403."""
        from nexus.contracts.exceptions import NexusPermissionError

        mock_fs.sys_readdir.side_effect = NexusPermissionError(
            path="/secret", message="Access denied"
        )

        resp = client.get("/glob", params={"pattern": "*.py", "path": "/secret"})
        assert resp.status_code == 403

    def test_glob_internal_error(self, client: TestClient, mock_fs: MagicMock) -> None:
        """Unexpected error returns 500."""
        mock_fs.sys_readdir.side_effect = RuntimeError("disk failure")

        resp = client.get("/glob", params={"pattern": "*.py"})
        assert resp.status_code == 500


# =============================================================================
# Grep Endpoint Tests
# =============================================================================


class TestGrepEndpoint:
    """Tests for GET /grep."""

    def test_grep_missing_pattern_returns_422(self, client: TestClient) -> None:
        """Missing required 'pattern' query param returns 422 (FastAPI validation)."""
        resp = client.get("/grep")
        assert resp.status_code == 422

    @patch("nexus.server.api.v2.routers.async_files.grep_files_mmap")
    def test_grep_happy_path_rust(self, mock_grep: MagicMock, client: TestClient) -> None:
        """Grep returns matches from Rust mmap grep."""
        mock_grep.return_value = [
            {"file": "/src/main.py", "line": 1, "content": "import os", "match": "import"},
            {"file": "/src/utils.py", "line": 1, "content": "import sys", "match": "import"},
        ]

        resp = client.get("/grep", params={"pattern": "import"})
        assert resp.status_code == 200

        data = resp.json()
        assert len(data["matches"]) == 2
        assert data["matches"][0]["file"] == "/src/main.py"
        assert data["matches"][0]["line"] == 1
        assert data["matches"][0]["content"] == "import os"
        assert data["matches"][0]["match"] == "import"
        assert data["total"] == 2
        assert data["truncated"] is False
        assert data["pattern"] == "import"
        assert data["base_path"] == "/"

    @patch("nexus.server.api.v2.routers.async_files.grep_files_mmap")
    def test_grep_with_ignore_case(self, mock_grep: MagicMock, client: TestClient) -> None:
        """ignore_case parameter is forwarded to grep_files_mmap."""
        mock_grep.return_value = []

        resp = client.get("/grep", params={"pattern": "README", "ignore_case": "true"})
        assert resp.status_code == 200

        # Verify ignore_case was passed as True
        mock_grep.assert_called_once()
        call_args = mock_grep.call_args
        # grep_files_mmap(pattern, file_paths, ignore_case, max_results)
        assert call_args[0][2] is True  # ignore_case

    @patch("nexus.server.api.v2.routers.async_files.grep_files_mmap")
    def test_grep_truncation(self, mock_grep: MagicMock, client: TestClient) -> None:
        """When Rust returns limit results, truncated=True."""
        mock_grep.return_value = [
            {"file": f"/file{i}.py", "line": 1, "content": "match", "match": "match"}
            for i in range(10)
        ]

        resp = client.get("/grep", params={"pattern": "match", "limit": 10})
        assert resp.status_code == 200

        data = resp.json()
        assert len(data["matches"]) == 10
        assert data["total"] == 10
        assert data["truncated"] is True

    @patch("nexus.server.api.v2.routers.async_files.grep_files_mmap")
    def test_grep_python_fallback(self, mock_grep: MagicMock, client: TestClient) -> None:
        """When Rust grep returns None, falls back to Python re."""
        mock_grep.return_value = None  # Rust unavailable

        resp = client.get("/grep", params={"pattern": "import"})
        assert resp.status_code == 200

        data = resp.json()
        # Should find 'import' in main.py and utils.py via Python fallback
        assert data["total"] >= 2
        files_matched = [m["file"] for m in data["matches"]]
        assert "/src/main.py" in files_matched
        assert "/src/utils.py" in files_matched

    @patch("nexus.server.api.v2.routers.async_files.grep_files_mmap")
    def test_grep_python_fallback_invalid_regex(
        self, mock_grep: MagicMock, client: TestClient
    ) -> None:
        """Invalid regex pattern returns 400 in Python fallback path."""
        mock_grep.return_value = None  # Rust unavailable, trigger Python fallback

        resp = client.get("/grep", params={"pattern": "[invalid"})
        assert resp.status_code == 400
        assert "Invalid regex" in resp.json()["detail"]

    @patch("nexus.server.api.v2.routers.async_files.grep_files_mmap")
    def test_grep_no_matches(self, mock_grep: MagicMock, client: TestClient) -> None:
        """Grep with no matches returns empty list."""
        mock_grep.return_value = []

        resp = client.get("/grep", params={"pattern": "nonexistent_string_xyz"})
        assert resp.status_code == 200

        data = resp.json()
        assert data["matches"] == []
        assert data["total"] == 0
        assert data["truncated"] is False

    def test_grep_limit_max_validation(self, client: TestClient) -> None:
        """Limit exceeding 1000 returns 422."""
        resp = client.get("/grep", params={"pattern": "test", "limit": 2000})
        assert resp.status_code == 422

    def test_grep_limit_min_validation(self, client: TestClient) -> None:
        """Limit below 1 returns 422."""
        resp = client.get("/grep", params={"pattern": "test", "limit": 0})
        assert resp.status_code == 422

    def test_grep_permission_error(self, client: TestClient, mock_fs: MagicMock) -> None:
        """Permission error on readdir returns 403."""
        from nexus.contracts.exceptions import NexusPermissionError

        mock_fs.sys_readdir.side_effect = NexusPermissionError(
            path="/secret", message="Access denied"
        )

        resp = client.get("/grep", params={"pattern": "test", "path": "/secret"})
        assert resp.status_code == 403

    def test_grep_internal_error(self, client: TestClient, mock_fs: MagicMock) -> None:
        """Unexpected error returns 500."""
        mock_fs.sys_readdir.side_effect = RuntimeError("disk failure")

        resp = client.get("/grep", params={"pattern": "test"})
        assert resp.status_code == 500

    @patch("nexus.server.api.v2.routers.async_files.grep_files_mmap")
    def test_grep_with_custom_base_path(
        self, mock_grep: MagicMock, client: TestClient, mock_fs: MagicMock
    ) -> None:
        """Grep passes base path to sys_readdir."""
        mock_grep.return_value = []

        resp = client.get("/grep", params={"pattern": "test", "path": "/src"})
        assert resp.status_code == 200

        mock_fs.sys_readdir.assert_called_once()
        call_args = mock_fs.sys_readdir.call_args
        assert (
            call_args[0][0] == "/src"
            or call_args[1].get("path") == "/src"
            or call_args[0] == ("/src",)
        )
