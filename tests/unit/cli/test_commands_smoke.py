"""Smoke tests for P0 CLI commands using Click's CliRunner.

These tests mock the filesystem layer and validate:
- Commands accept --json, -v, --quiet, --fields flags
- JSON output has correct envelope structure
- Timing data is included in JSON output
- Human output works without errors
"""

import json
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli.commands.directory import list_files, tree
from nexus.cli.commands.search import glob, grep


@pytest.fixture(autouse=True)
def _disable_auto_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable auto-JSON in CliRunner (stdout is not a TTY)."""
    monkeypatch.setenv("NEXUS_NO_AUTO_JSON", "1")


def _make_mock_nx() -> MagicMock:
    """Create a mock NexusFS with common methods."""
    nx = MagicMock()
    nx.close = MagicMock()
    # sys_readdir is sync in the real NexusFS
    nx.sys_readdir = MagicMock()
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

    # ------------------------------------------------------------------
    # #3701 CLI wiring: files=[...], context lines, invert_match
    # ------------------------------------------------------------------

    def test_grep_files_flag_forwarded(self) -> None:
        """Repeated --files flags are collected into a list and forwarded."""
        nx = _make_mock_nx()
        nx.service("search").grep.return_value = {"results": []}

        runner = CliRunner()
        with _patch_search_open_filesystem(nx):
            result = runner.invoke(
                grep,
                [
                    "TODO",
                    "--files",
                    "/src/a.py",
                    "--files",
                    "/src/b.py",
                    "--json",
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        kwargs = nx.service("search").grep.call_args.kwargs
        assert kwargs["files"] == ["/src/a.py", "/src/b.py"]

    def test_grep_files_absent_omits_kwarg(self) -> None:
        """Without --files the kwarg must not appear at all (not files=[])."""
        nx = _make_mock_nx()
        nx.service("search").grep.return_value = {"results": []}

        runner = CliRunner()
        with _patch_search_open_filesystem(nx):
            runner.invoke(grep, ["TODO", "--json"], catch_exceptions=False)

        kwargs = nx.service("search").grep.call_args.kwargs
        assert "files" not in kwargs

    def test_grep_files_from_stdin(self) -> None:
        """--files-from=- reads newline-separated paths from stdin."""
        nx = _make_mock_nx()
        nx.service("search").grep.return_value = {"results": []}

        runner = CliRunner()
        with _patch_search_open_filesystem(nx):
            result = runner.invoke(
                grep,
                ["TODO", "--files-from", "-", "--json"],
                input="/src/a.py\n/src/b.py\n/src/c.py\n",
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        kwargs = nx.service("search").grep.call_args.kwargs
        assert kwargs["files"] == ["/src/a.py", "/src/b.py", "/src/c.py"]

    def test_grep_files_from_stdin_skips_blank_and_comments(self) -> None:
        """Blank lines and ``#`` comments in --files-from are stripped."""
        nx = _make_mock_nx()
        nx.service("search").grep.return_value = {"results": []}

        runner = CliRunner()
        with _patch_search_open_filesystem(nx):
            runner.invoke(
                grep,
                ["TODO", "--files-from", "-", "--json"],
                input="# header\n/src/a.py\n\n/src/b.py\n# comment\n",
                catch_exceptions=False,
            )

        kwargs = nx.service("search").grep.call_args.kwargs
        assert kwargs["files"] == ["/src/a.py", "/src/b.py"]

    def test_grep_files_and_files_from_merge(self) -> None:
        """Explicit --files values and --files-from stdin merge into one list."""
        nx = _make_mock_nx()
        nx.service("search").grep.return_value = {"results": []}

        runner = CliRunner()
        with _patch_search_open_filesystem(nx):
            runner.invoke(
                grep,
                [
                    "TODO",
                    "--files",
                    "/explicit/a.py",
                    "--files-from",
                    "-",
                    "--json",
                ],
                input="/from-stdin/b.py\n",
                catch_exceptions=False,
            )

        kwargs = nx.service("search").grep.call_args.kwargs
        assert kwargs["files"] == ["/explicit/a.py", "/from-stdin/b.py"]

    def test_grep_before_and_after_context_forwarded(self) -> None:
        """-B and -A flags are forwarded to the RPC call."""
        nx = _make_mock_nx()
        nx.service("search").grep.return_value = {"results": []}

        runner = CliRunner()
        with _patch_search_open_filesystem(nx):
            runner.invoke(
                grep,
                ["TODO", "-B", "3", "-A", "2", "--json"],
                catch_exceptions=False,
            )

        kwargs = nx.service("search").grep.call_args.kwargs
        assert kwargs["before_context"] == 3
        assert kwargs["after_context"] == 2

    def test_grep_context_flag_sets_both_before_and_after(self) -> None:
        """-C / --context is shorthand for -A and -B with the same value."""
        nx = _make_mock_nx()
        nx.service("search").grep.return_value = {"results": []}

        runner = CliRunner()
        with _patch_search_open_filesystem(nx):
            runner.invoke(
                grep,
                ["TODO", "-C", "5", "--json"],
                catch_exceptions=False,
            )

        kwargs = nx.service("search").grep.call_args.kwargs
        assert kwargs["before_context"] == 5
        assert kwargs["after_context"] == 5

    def test_grep_invert_match_forwarded(self) -> None:
        """--invert-match flag is forwarded to the RPC call."""
        nx = _make_mock_nx()
        nx.service("search").grep.return_value = {"results": []}

        runner = CliRunner()
        with _patch_search_open_filesystem(nx):
            runner.invoke(
                grep,
                ["TODO", "--invert-match", "--json"],
                catch_exceptions=False,
            )

        kwargs = nx.service("search").grep.call_args.kwargs
        assert kwargs["invert_match"] is True

    def test_grep_no_context_flags_omits_kwargs(self) -> None:
        """When no context flags are set, the RPC call stays lean."""
        nx = _make_mock_nx()
        nx.service("search").grep.return_value = {"results": []}

        runner = CliRunner()
        with _patch_search_open_filesystem(nx):
            runner.invoke(grep, ["TODO", "--json"], catch_exceptions=False)

        kwargs = nx.service("search").grep.call_args.kwargs
        assert "before_context" not in kwargs
        assert "after_context" not in kwargs
        assert "invert_match" not in kwargs

    def test_grep_l_mode_prints_plain_filenames_for_piping(self) -> None:
        """-l output must be one filename per line, unadorned, pipe-safe."""
        nx = _make_mock_nx()
        nx.service("search").grep.return_value = {
            "results": [
                {"file": "/src/a.py", "line": 1, "content": "x"},
                {"file": "/src/b.py", "line": 1, "content": "x"},
                {"file": "/src/a.py", "line": 5, "content": "x"},  # duplicate file
            ]
        }

        runner = CliRunner()
        with _patch_search_open_filesystem(nx):
            result = runner.invoke(grep, ["x", "-l"], catch_exceptions=False)

        assert result.exit_code == 0
        # Plain output — no Rich markup, sorted, deduped by file
        lines = [ln for ln in result.output.split("\n") if ln]
        assert "/src/a.py" in lines
        assert "/src/b.py" in lines
        # No Rich markup in piped output
        assert "[" not in result.output
        assert "nexus.warning" not in result.output


class TestGlobFilesWiring:
    """#3701 CLI wiring for glob: --files, --files-from, --plain."""

    def test_glob_files_flag_forwarded(self) -> None:
        nx = _make_mock_nx()
        nx.service("search").glob.return_value = {"matches": []}

        runner = CliRunner()
        with _patch_search_open_filesystem(nx):
            runner.invoke(
                glob,
                ["*.py", "--files", "/src/a.py", "--files", "/src/b.py", "--json"],
                catch_exceptions=False,
            )

        kwargs = nx.service("search").glob.call_args.kwargs
        assert kwargs["files"] == ["/src/a.py", "/src/b.py"]

    def test_glob_files_from_stdin(self) -> None:
        nx = _make_mock_nx()
        nx.service("search").glob.return_value = {"matches": []}

        runner = CliRunner()
        with _patch_search_open_filesystem(nx):
            runner.invoke(
                glob,
                ["*.py", "--files-from", "-", "--json"],
                input="/src/a.py\n/src/b.py\n",
                catch_exceptions=False,
            )

        kwargs = nx.service("search").glob.call_args.kwargs
        assert kwargs["files"] == ["/src/a.py", "/src/b.py"]

    def test_glob_files_absent_omits_kwarg(self) -> None:
        nx = _make_mock_nx()
        nx.service("search").glob.return_value = {"matches": []}

        runner = CliRunner()
        with _patch_search_open_filesystem(nx):
            runner.invoke(glob, ["*.py", "--json"], catch_exceptions=False)

        kwargs = nx.service("search").glob.call_args.kwargs
        assert "files" not in kwargs

    def test_glob_plain_mode_pipe_safe_output(self) -> None:
        """--plain output must be one path per line, unadorned."""
        nx = _make_mock_nx()
        nx.service("search").glob.return_value = {"matches": ["/src/a.py", "/src/b.py"]}

        runner = CliRunner()
        with _patch_search_open_filesystem(nx):
            result = runner.invoke(glob, ["*.py", "--plain"], catch_exceptions=False)

        assert result.exit_code == 0
        lines = [ln for ln in result.output.split("\n") if ln]
        assert "/src/a.py" in lines
        assert "/src/b.py" in lines
        assert "[" not in result.output  # no Rich markup


class TestResolveFilesArgHelper:
    """Unit tests for the _resolve_files_arg helper (#3701)."""

    def test_no_flags_returns_none(self) -> None:
        from nexus.cli.commands.search import _resolve_files_arg

        assert _resolve_files_arg(files=(), files_from=None) is None

    def test_files_only(self) -> None:
        from nexus.cli.commands.search import _resolve_files_arg

        result = _resolve_files_arg(files=("/a", "/b"), files_from=None)
        assert result == ["/a", "/b"]

    def test_files_from_file(self, tmp_path) -> None:
        from nexus.cli.commands.search import _resolve_files_arg

        p = tmp_path / "list.txt"
        p.write_text("/x\n/y\n/z\n")
        result = _resolve_files_arg(files=(), files_from=str(p))
        assert result == ["/x", "/y", "/z"]

    def test_files_from_file_strips_blanks_and_comments(self, tmp_path) -> None:
        from nexus.cli.commands.search import _resolve_files_arg

        p = tmp_path / "list.txt"
        p.write_text("# header\n/x\n\n/y\n  # indented comment (not stripped)\n")
        result = _resolve_files_arg(files=(), files_from=str(p))
        # The indented ``#`` comment IS stripped because we .strip() first
        assert result == ["/x", "/y"]

    def test_merge_explicit_then_file(self, tmp_path) -> None:
        from nexus.cli.commands.search import _resolve_files_arg

        p = tmp_path / "list.txt"
        p.write_text("/from-file\n")
        result = _resolve_files_arg(files=("/explicit",), files_from=str(p))
        assert result == ["/explicit", "/from-file"]

    def test_empty_files_from_returns_empty_list(self, tmp_path) -> None:
        """An empty file produces [] (not None) so the server-side
        empty-list short-circuit still fires."""
        from nexus.cli.commands.search import _resolve_files_arg

        p = tmp_path / "empty.txt"
        p.write_text("")
        result = _resolve_files_arg(files=(), files_from=str(p))
        assert result == []
