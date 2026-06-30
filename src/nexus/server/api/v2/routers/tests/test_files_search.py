"""Tests for GET /glob and GET /grep file search endpoints."""

import threading
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus._rust_compat import grep_files_mmap
from nexus.contracts.exceptions import AccessDeniedError, NexusPermissionError
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
    fs.service.return_value = None
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

    def test_glob_builtin_permission_error(self, client: TestClient, mock_fs: MagicMock) -> None:
        """Built-in PermissionError on readdir returns 403."""
        mock_fs.sys_readdir.side_effect = PermissionError("Access denied")

        resp = client.get("/glob", params={"pattern": "*.py", "path": "/secret"})
        assert resp.status_code == 403

    def test_glob_internal_error(self, client: TestClient, mock_fs: MagicMock) -> None:
        """Unexpected error returns 500."""
        mock_fs.sys_readdir.side_effect = RuntimeError("disk failure")

        resp = client.get("/glob", params={"pattern": "*.py"})
        assert resp.status_code == 500

    @patch("nexus.server.api.v2.routers.async_files.glob_filter")
    def test_glob_sync_readdir_runs_off_event_loop(
        self,
        mock_glob_filter: MagicMock,
        client: TestClient,
        mock_fs: MagicMock,
    ) -> None:
        """Recursive synchronous listings do not block the zone event loop."""
        event_loop_threads: list[int] = []
        listing_threads: list[int] = []

        def _readdir(*_args: object, **_kwargs: object) -> list[str]:
            listing_threads.append(threading.get_ident())
            return ["/remote.txt"]

        async def _auth_result() -> dict[str, object]:
            event_loop_threads.append(threading.get_ident())
            return {
                "authenticated": True,
                "user_id": "test-user",
                "groups": [],
                "zone_id": "root",
                "is_admin": False,
            }

        mock_fs.sys_readdir.side_effect = _readdir
        mock_glob_filter.return_value = ["/remote.txt"]
        cast(FastAPI, client.app).dependency_overrides[get_auth_result] = _auth_result

        resp = client.get("/glob", params={"pattern": "*.txt"})

        assert resp.status_code == 200
        assert listing_threads
        assert event_loop_threads
        assert listing_threads[0] != event_loop_threads[0]


# =============================================================================
# Grep Endpoint Tests
# =============================================================================


class TestGrepEndpoint:
    """Tests for GET /grep."""

    def test_grep_missing_pattern_returns_422(self, client: TestClient) -> None:
        """Missing required 'pattern' query param returns 422 (FastAPI validation)."""
        resp = client.get("/grep")
        assert resp.status_code == 422

    def test_grep_derives_match_from_real_mmap_service_result(
        self,
        client: TestClient,
        mock_fs: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Real mmap output without ``match`` remains a valid API result."""
        virtual_path = "/src/main.py"
        local_path = tmp_path / "main.py"
        local_path.write_text("before\nimport os\nafter\n", encoding="utf-8")

        class ProductionShapedSearchService:
            async def grep(self, **kwargs: Any) -> list[dict[str, Any]]:
                assert kwargs["files"] == [virtual_path]
                results = grep_files_mmap(
                    str(kwargs["pattern"]),
                    [str(local_path)],
                    ignore_case=bool(kwargs["ignore_case"]),
                    max_results=int(kwargs["max_results"]),
                )
                for result in results:
                    result["file"] = virtual_path
                return results

        mock_fs.sys_readdir.return_value = [virtual_path]
        mock_fs.service.return_value = ProductionShapedSearchService()

        resp = client.get("/grep", params={"pattern": r"imp\w+", "limit": 5})

        assert resp.status_code == 200
        assert resp.json()["matches"] == [
            {
                "file": virtual_path,
                "line": 2,
                "content": "import os",
                "match": "import",
            }
        ]

    def test_grep_delegates_virtual_working_set_context_and_limit(
        self,
        client: TestClient,
        mock_fs: MagicMock,
    ) -> None:
        """The router delegates virtual candidates to SearchService."""
        virtual_paths = ["/src/main.py", "/connector/remote.py"]
        search = MagicMock()
        search.grep = AsyncMock(
            return_value=[
                {
                    "file": "/connector/remote.py",
                    "line": 3,
                    "content": "prefix needle suffix",
                    "match": "needle",
                }
            ]
        )
        mock_fs.sys_readdir.return_value = virtual_paths
        mock_fs.service.return_value = search

        resp = client.get("/grep", params={"pattern": "needle", "limit": 7})

        assert resp.status_code == 200
        assert resp.json()["matches"][0]["file"] == "/connector/remote.py"
        search.grep.assert_awaited_once()
        kwargs = search.grep.await_args.kwargs
        assert kwargs["files"] == virtual_paths
        assert kwargs["max_results"] == 7
        assert kwargs["context"].zone_id == "root"

    def test_grep_virtual_host_looking_path_never_uses_host_path_authority(
        self,
        client: TestClient,
        mock_fs: MagicMock,
        tmp_path: Path,
    ) -> None:
        """A connector key that names a real host file remains virtual."""
        host_looking_key = str(tmp_path / "remote-key.txt")
        (tmp_path / "remote-key.txt").write_text("host-only needle\n", encoding="utf-8")
        search = MagicMock()
        search.grep = AsyncMock(return_value=[])
        mock_fs.sys_readdir.return_value = [host_looking_key]
        mock_fs.service.return_value = search

        with patch("pathlib.Path.is_file", side_effect=AssertionError("host path probe")):
            resp = client.get("/grep", params={"pattern": "needle"})

        assert resp.status_code == 200
        assert resp.json()["matches"] == []
        assert search.grep.await_args.kwargs["files"] == [host_looking_key]
        mock_fs.read.assert_not_called()

    def test_grep_sync_vfs_listing_and_read_run_off_event_loop(
        self,
        client: TestClient,
        mock_fs: MagicMock,
    ) -> None:
        """Synchronous VFS listing and reads do not block the request event loop."""
        event_loop_threads: list[int] = []
        listing_threads: list[int] = []
        read_threads: list[int] = []

        def _readdir(*_args: object, **_kwargs: object) -> list[str]:
            listing_threads.append(threading.get_ident())
            return ["/remote.txt"]

        def _read(*_args: object, **_kwargs: object) -> bytes:
            read_threads.append(threading.get_ident())
            return b"needle\n"

        def _service(*_args: object, **_kwargs: object) -> None:
            event_loop_threads.append(threading.get_ident())

        mock_fs.sys_readdir.side_effect = _readdir
        mock_fs.read.side_effect = _read
        mock_fs.service.side_effect = _service

        resp = client.get("/grep", params={"pattern": "needle"})

        assert resp.status_code == 200
        assert resp.json()["total"] == 1
        assert listing_threads[0] != event_loop_threads[0]
        assert read_threads[0] != event_loop_threads[0]

    def test_grep_async_vfs_listing_is_awaited(
        self,
        client: TestClient,
        mock_fs: MagicMock,
    ) -> None:
        """A native async recursive listing is awaited."""
        mock_fs.sys_readdir = AsyncMock(return_value=["/remote.txt"])
        mock_fs.read.side_effect = None
        mock_fs.read.return_value = b"needle\n"

        resp = client.get("/grep", params={"pattern": "needle"})

        assert resp.status_code == 200
        assert resp.json()["total"] == 1
        mock_fs.sys_readdir.assert_awaited_once()

    def test_grep_async_vfs_read_is_awaited(
        self,
        client: TestClient,
        mock_fs: MagicMock,
    ) -> None:
        """A native async VFS reader is awaited on fallback."""
        mock_fs.sys_readdir.return_value = ["/remote.txt"]
        mock_fs.read = AsyncMock(return_value=b"needle\n")

        resp = client.get("/grep", params={"pattern": "needle"})

        assert resp.status_code == 200
        assert resp.json()["total"] == 1
        mock_fs.read.assert_awaited_once()

    def test_grep_happy_path_search_service(
        self,
        client: TestClient,
        mock_fs: MagicMock,
    ) -> None:
        """Grep returns virtual-path matches from SearchService."""
        search = MagicMock()
        search.grep = AsyncMock(
            return_value=[
                {
                    "file": "/src/main.py",
                    "line": 1,
                    "content": "import os",
                    "match": "import",
                },
                {
                    "file": "/src/utils.py",
                    "line": 1,
                    "content": "import sys",
                    "match": "import",
                },
            ]
        )
        mock_fs.service.return_value = search

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

    def test_grep_with_ignore_case(
        self,
        client: TestClient,
        mock_fs: MagicMock,
    ) -> None:
        """ignore_case parameter is forwarded to SearchService."""
        search = MagicMock()
        search.grep = AsyncMock(return_value=[])
        mock_fs.service.return_value = search

        resp = client.get("/grep", params={"pattern": "README", "ignore_case": "true"})
        assert resp.status_code == 200

        search.grep.assert_awaited_once()
        assert search.grep.await_args.kwargs["ignore_case"] is True

    def test_grep_truncation(
        self,
        client: TestClient,
        mock_fs: MagicMock,
    ) -> None:
        """When SearchService returns limit results, truncated=True."""
        search = MagicMock()
        search.grep = AsyncMock(
            return_value=[
                {
                    "file": f"/file{i}.py",
                    "line": 1,
                    "content": "match",
                    "match": "match",
                }
                for i in range(10)
            ]
        )
        mock_fs.service.return_value = search

        resp = client.get("/grep", params={"pattern": "match", "limit": 10})
        assert resp.status_code == 200

        data = resp.json()
        assert len(data["matches"]) == 10
        assert data["total"] == 10
        assert data["truncated"] is True

    def test_grep_python_fallback(self, client: TestClient) -> None:
        """Virtual paths are searched through fs.read() without SearchService."""
        resp = client.get("/grep", params={"pattern": "import"})
        assert resp.status_code == 200

        data = resp.json()
        # Should find 'import' in main.py and utils.py via Python fallback
        assert data["total"] >= 2
        files_matched = [m["file"] for m in data["matches"]]
        assert "/src/main.py" in files_matched
        assert "/src/utils.py" in files_matched

    def test_grep_preserves_service_mixed_results_under_global_limit(
        self,
        client: TestClient,
        mock_fs: MagicMock,
    ) -> None:
        """SearchService's merged lanes stay virtual and share one limit."""
        search = MagicMock()
        search.grep = AsyncMock(
            return_value=[
                {
                    "file": "/local.py",
                    "line": 1,
                    "content": "import local",
                    "match": "service-local",
                },
                {
                    "file": "/virtual.py",
                    "line": 1,
                    "content": "import virtual",
                    "match": "service-virtual",
                },
            ]
        )
        mock_fs.sys_readdir.return_value = ["/local.py", "/virtual.py"]
        mock_fs.service.return_value = search

        resp = client.get("/grep", params={"pattern": "import", "limit": 2})
        assert resp.status_code == 200

        data = resp.json()
        assert [match["file"] for match in data["matches"]] == ["/local.py", "/virtual.py"]
        assert [match["match"] for match in data["matches"]] == [
            "service-local",
            "service-virtual",
        ]
        assert search.grep.await_args.kwargs["max_results"] == 2
        mock_fs.read.assert_not_called()

    def test_grep_empty_service_results_are_valid_without_fallback(
        self,
        client: TestClient,
        mock_fs: MagicMock,
    ) -> None:
        """An empty SearchService response does not trigger a second scan."""
        search = MagicMock()
        search.grep = AsyncMock(return_value=[])
        mock_fs.sys_readdir.return_value = ["/virtual.py"]
        mock_fs.read.return_value = b"needle virtual\n"
        mock_fs.service.return_value = search

        resp = client.get("/grep", params={"pattern": "needle"})
        assert resp.status_code == 200

        data = resp.json()
        assert data["matches"] == []
        mock_fs.read.assert_not_called()

    def test_grep_python_fallback_invalid_regex(
        self, client: TestClient, mock_fs: MagicMock
    ) -> None:
        """Invalid regex pattern returns 400 before listing or searching."""
        resp = client.get("/grep", params={"pattern": "[invalid"})
        assert resp.status_code == 400
        assert "Invalid regex" in resp.json()["detail"]
        mock_fs.sys_readdir.assert_not_called()
        mock_fs.service.assert_not_called()

    def test_grep_no_matches(self, client: TestClient, mock_fs: MagicMock) -> None:
        """Grep with no matches returns empty list."""
        search = MagicMock()
        search.grep = AsyncMock(return_value=[])
        mock_fs.service.return_value = search

        resp = client.get("/grep", params={"pattern": "nonexistent_string_xyz"})
        assert resp.status_code == 200

        data = resp.json()
        assert data["matches"] == []
        assert data["total"] == 0
        assert data["truncated"] is False
        mock_fs.read.assert_not_called()

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

    def test_grep_builtin_permission_error(self, client: TestClient, mock_fs: MagicMock) -> None:
        """Built-in PermissionError on readdir returns 403."""
        mock_fs.sys_readdir.side_effect = PermissionError("Access denied")

        resp = client.get("/grep", params={"pattern": "test", "path": "/secret"})
        assert resp.status_code == 403

    @pytest.mark.parametrize(
        "error",
        [
            PermissionError("Access denied"),
            NexusPermissionError(path="/secret.txt", message="Access denied"),
            AccessDeniedError(message="Access denied", path="/secret.txt"),
        ],
        ids=["builtin", "nexus", "namespace"],
    )
    def test_grep_virtual_read_permission_errors_return_403(
        self,
        client: TestClient,
        mock_fs: MagicMock,
        error: Exception,
    ) -> None:
        """Permission failures while reading a virtual file are not skipped."""
        mock_fs.sys_readdir.return_value = ["/secret.txt"]
        mock_fs.read.side_effect = error

        resp = client.get("/grep", params={"pattern": "secret", "path": "/"})

        assert resp.status_code == 403

    @pytest.mark.parametrize(
        "error",
        [
            PermissionError("Access denied"),
            NexusPermissionError(path="/secret.txt", message="Access denied"),
            AccessDeniedError(message="Access denied", path="/secret.txt"),
        ],
        ids=["builtin", "nexus", "namespace"],
    )
    def test_grep_search_service_permission_errors_return_403(
        self,
        client: TestClient,
        mock_fs: MagicMock,
        error: Exception,
    ) -> None:
        """Authorization failures from SearchService are not swallowed."""
        search = MagicMock()
        search.grep = AsyncMock(side_effect=error)
        mock_fs.sys_readdir.return_value = ["/secret.txt"]
        mock_fs.service.return_value = search

        resp = client.get("/grep", params={"pattern": "secret", "path": "/"})

        assert resp.status_code == 403
        mock_fs.read.assert_not_called()

    def test_grep_search_service_non_permission_error_uses_virtual_read(
        self,
        client: TestClient,
        mock_fs: MagicMock,
    ) -> None:
        """Ordinary SearchService failures preserve the VFS fallback."""
        search = MagicMock()
        search.grep = AsyncMock(side_effect=RuntimeError("search unavailable"))
        mock_fs.sys_readdir.return_value = ["/src/main.py"]
        mock_fs.service.return_value = search

        resp = client.get("/grep", params={"pattern": "import", "path": "/"})

        assert resp.status_code == 200
        assert [match["file"] for match in resp.json()["matches"]] == ["/src/main.py"]

    def test_grep_virtual_read_non_permission_error_skips_only_failed_file(
        self,
        client: TestClient,
        mock_fs: MagicMock,
    ) -> None:
        """An ordinary read failure does not suppress later valid matches."""
        mock_fs.sys_readdir.return_value = ["/broken.txt", "/src/main.py"]

        def _read(path: str, **_kwargs: object) -> bytes:
            if path == "/broken.txt":
                raise RuntimeError("unreadable")
            return b"import os\n"

        mock_fs.read.side_effect = _read

        resp = client.get("/grep", params={"pattern": "import", "path": "/"})

        assert resp.status_code == 200
        assert [match["file"] for match in resp.json()["matches"]] == ["/src/main.py"]

    def test_grep_internal_error(self, client: TestClient, mock_fs: MagicMock) -> None:
        """Unexpected error returns 500."""
        mock_fs.sys_readdir.side_effect = RuntimeError("disk failure")

        resp = client.get("/grep", params={"pattern": "test"})
        assert resp.status_code == 500

    def test_grep_with_custom_base_path(self, client: TestClient, mock_fs: MagicMock) -> None:
        """Grep passes base path to readdir and SearchService."""
        search = MagicMock()
        search.grep = AsyncMock(return_value=[])
        mock_fs.service.return_value = search

        resp = client.get("/grep", params={"pattern": "test", "path": "/src"})
        assert resp.status_code == 200

        mock_fs.sys_readdir.assert_called_once()
        call_args = mock_fs.sys_readdir.call_args
        assert (
            call_args[0][0] == "/src"
            or call_args[1].get("path") == "/src"
            or call_args[0] == ("/src",)
        )
        assert search.grep.await_args.kwargs["path"] == "/src"
