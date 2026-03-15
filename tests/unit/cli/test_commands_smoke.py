"""Smoke tests for P0 CLI commands using Click's CliRunner.

These tests mock the filesystem layer and validate:
- Commands accept --json, -v, --quiet, --fields flags
- JSON output has correct envelope structure
- Timing data is included in JSON output
- Human output works without errors
"""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli.commands.directory import list_files, tree
from nexus.cli.commands.search import glob, grep


@pytest.fixture(autouse=True)
def _disable_auto_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable auto-JSON in CliRunner (stdout is not a TTY)."""
    monkeypatch.setenv("NEXUS_NO_AUTO_JSON", "1")


def _make_mock_nx() -> MagicMock:
    """Create a mock NexusFilesystem with common methods."""
    nx = MagicMock()
    nx.close = MagicMock()
    # sys_readdir is async in the real NexusFilesystem
    nx.sys_readdir = AsyncMock()
    return nx


def _patch_open_filesystem(nx: MagicMock):
    """Patch open_filesystem to yield the mock."""

    @asynccontextmanager
    async def _mock_open(remote_url=None, remote_api_key=None, **kwargs):
        yield nx

    return patch("nexus.cli.commands.directory.open_filesystem", _mock_open)


def _patch_search_open_filesystem(nx: MagicMock):
    """Patch open_filesystem for search module."""

    @asynccontextmanager
    async def _mock_open(remote_url=None, remote_api_key=None, **kwargs):
        yield nx

    return patch("nexus.cli.commands.search.open_filesystem", _mock_open)


class TestLsCommand:
    """nexus ls - list files."""

    def test_ls_json_output(self) -> None:
        nx = _make_mock_nx()
        nx.sys_readdir.return_value = [
            {
                "path": "/workspace/file.txt",
                "size": 100,
                "is_directory": False,
                "modified_at": None,
            },
            {"path": "/workspace/data/", "size": 0, "is_directory": True, "modified_at": None},
        ]

        runner = CliRunner()
        with _patch_open_filesystem(nx):
            result = runner.invoke(list_files, ["/workspace", "--json"], catch_exceptions=False)

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert "data" in output
        assert "_timing" in output
        assert len(output["data"]) == 2
        assert output["data"][0]["path"] == "/workspace/file.txt"
        assert output["data"][0]["type"] == "file"
        assert output["data"][1]["type"] == "directory"

    def test_ls_json_with_fields_filter(self) -> None:
        nx = _make_mock_nx()
        nx.sys_readdir.return_value = [
            {"path": "/a.txt", "size": 50, "is_directory": False, "modified_at": None},
        ]

        runner = CliRunner()
        with _patch_open_filesystem(nx):
            result = runner.invoke(
                list_files, ["/", "--json", "--fields", "path"], catch_exceptions=False
            )

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["data"] == [{"path": "/a.txt"}]

    def test_ls_json_timing_has_phases(self) -> None:
        nx = _make_mock_nx()
        nx.sys_readdir.return_value = [
            {"path": "/a.txt", "size": 10, "is_directory": False, "modified_at": None},
        ]

        runner = CliRunner()
        with _patch_open_filesystem(nx):
            result = runner.invoke(list_files, ["/", "--json"], catch_exceptions=False)

        output = json.loads(result.output)
        timing = output["_timing"]
        assert "total_ms" in timing
        assert timing["total_ms"] > 0
        assert "phases" in timing
        assert "server" in timing["phases"]

    def test_ls_human_output(self) -> None:
        nx = _make_mock_nx()
        nx.sys_readdir.return_value = [
            {
                "path": "/workspace/file.txt",
                "size": 100,
                "is_directory": False,
                "modified_at": None,
            },
        ]

        runner = CliRunner()
        with _patch_open_filesystem(nx):
            result = runner.invoke(list_files, ["/workspace"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "/workspace/file.txt" in result.output

    def test_ls_empty_directory(self) -> None:
        nx = _make_mock_nx()
        nx.sys_readdir.return_value = []

        runner = CliRunner()
        with _patch_open_filesystem(nx):
            result = runner.invoke(list_files, ["/empty", "--json"], catch_exceptions=False)

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["data"] == []

    def test_ls_quiet_suppresses_output(self) -> None:
        nx = _make_mock_nx()
        nx.sys_readdir.return_value = [
            {"path": "/a.txt", "size": 10, "is_directory": False, "modified_at": None},
        ]

        runner = CliRunner()
        with _patch_open_filesystem(nx):
            result = runner.invoke(list_files, ["/", "--quiet"], catch_exceptions=False)

        assert result.exit_code == 0
        assert result.output == ""

    def test_ls_verbose_shows_timing_in_json(self) -> None:
        """With -v, timing is included; verify via --json which captures it in envelope."""
        nx = _make_mock_nx()
        nx.sys_readdir.return_value = [
            {"path": "/a.txt", "size": 10, "is_directory": False, "modified_at": None},
        ]

        runner = CliRunner()
        with _patch_open_filesystem(nx):
            result = runner.invoke(list_files, ["/", "--json", "-v"], catch_exceptions=False)

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["_timing"]["total_ms"] > 0
        assert "server" in output["_timing"]["phases"]


class TestTreeCommand:
    """nexus tree - directory tree."""

    def test_tree_json_output(self) -> None:
        nx = _make_mock_nx()
        nx.sys_readdir.return_value = [
            {"path": "/src/main.py", "size": 500, "is_directory": False, "modified_at": None},
            {"path": "/src/lib/", "size": 0, "is_directory": True, "modified_at": None},
        ]

        runner = CliRunner()
        with _patch_open_filesystem(nx):
            result = runner.invoke(tree, ["/", "--json"], catch_exceptions=False)

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert "data" in output
        assert output["data"]["root"] == "/"
        assert output["data"]["total_files"] == 2
        assert "_timing" in output


class TestGlobCommand:
    """nexus glob - find files by pattern."""

    def test_glob_json_output(self) -> None:
        nx = _make_mock_nx()
        nx.service("search").glob.return_value = {"matches": ["/src/main.py", "/src/utils.py"]}

        runner = CliRunner()
        with _patch_search_open_filesystem(nx):
            result = runner.invoke(glob, ["**/*.py", "--json"], catch_exceptions=False)

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert len(output["data"]) == 2
        assert output["data"][0]["path"] == "/src/main.py"
        assert "_timing" in output

    def test_glob_no_matches(self) -> None:
        nx = _make_mock_nx()
        nx.service("search").glob.return_value = {"matches": []}

        runner = CliRunner()
        with _patch_search_open_filesystem(nx):
            result = runner.invoke(glob, ["**/*.xyz", "--json"], catch_exceptions=False)

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["data"] == []


class TestGrepCommand:
    """nexus grep - search file contents."""

    def test_grep_json_output(self) -> None:
        nx = _make_mock_nx()
        nx.service("search").grep.return_value = {
            "results": [
                {"file": "/src/main.py", "line": 10, "content": "# TODO: fix this"},
                {"file": "/src/main.py", "line": 20, "content": "# TODO: refactor"},
            ]
        }

        runner = CliRunner()
        with _patch_search_open_filesystem(nx):
            result = runner.invoke(grep, ["TODO", "--json"], catch_exceptions=False)

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["data"]["total_matches"] == 2
        assert output["data"]["files_matched"] == 1
        assert "_timing" in output

    def test_grep_no_matches(self) -> None:
        nx = _make_mock_nx()
        nx.service("search").grep.return_value = {"results": []}

        runner = CliRunner()
        with _patch_search_open_filesystem(nx):
            result = runner.invoke(grep, ["NONEXISTENT", "--json"], catch_exceptions=False)

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["data"] == []

    def test_grep_human_with_line_numbers(self) -> None:
        nx = _make_mock_nx()
        nx.service("search").grep.return_value = {
            "results": [
                {"file": "/src/main.py", "line": 10, "content": "# TODO"},
            ]
        }

        runner = CliRunner()
        with _patch_search_open_filesystem(nx):
            result = runner.invoke(grep, ["TODO", "-n"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "10:" in result.output

    def test_grep_vvv_includes_request_id(self) -> None:
        nx = _make_mock_nx()
        nx.service("search").grep.return_value = {
            "results": [
                {"file": "/a.py", "line": 1, "content": "match"},
            ]
        }

        runner = CliRunner()
        with _patch_search_open_filesystem(nx):
            result = runner.invoke(grep, ["match", "--json", "-vvv"], catch_exceptions=False)

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert "_request_id" in output
        assert len(output["_request_id"]) == 32  # uuid hex
