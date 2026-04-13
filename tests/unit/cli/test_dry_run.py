"""Tests for dry-run infrastructure and per-command smoke tests."""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli.dry_run import add_dry_run_option, dry_run_preview, render_dry_run

# ---------------------------------------------------------------------------
# Unit tests for the decorator and helpers
# ---------------------------------------------------------------------------


class TestAddDryRunOption:
    def test_injects_dry_run_kwarg(self) -> None:
        import click

        @click.command()
        @add_dry_run_option
        def cmd(dry_run: bool) -> None:
            click.echo(f"dry_run={dry_run}")

        runner = CliRunner()
        result = runner.invoke(cmd, [])
        assert result.exit_code == 0
        assert "dry_run=False" in result.output

    def test_dry_run_flag(self) -> None:
        import click

        @click.command()
        @add_dry_run_option
        def cmd(dry_run: bool) -> None:
            click.echo(f"dry_run={dry_run}")

        runner = CliRunner()
        result = runner.invoke(cmd, ["--dry-run"])
        assert result.exit_code == 0
        assert "dry_run=True" in result.output


class TestDryRunPreview:
    def test_basic_preview(self) -> None:
        preview = dry_run_preview("write", path="/test.txt")
        assert preview["dry_run"] is True
        assert preview["operation"] == "write"
        assert preview["path"] == "/test.txt"

    def test_preview_with_source_dest(self) -> None:
        preview = dry_run_preview("cp", source="/a", dest="/b")
        assert preview["source"] == "/a"
        assert preview["dest"] == "/b"
        assert "path" not in preview

    def test_preview_with_details(self) -> None:
        preview = dry_run_preview(
            "write", path="/test.txt", details={"bytes": 42, "action": "create"}
        )
        assert preview["bytes"] == 42
        assert preview["action"] == "create"

    def test_preview_no_optional_fields(self) -> None:
        preview = dry_run_preview("rm")
        assert "path" not in preview
        assert "source" not in preview
        assert "dest" not in preview


class TestRenderDryRun:
    def test_human_mode(self, capsys: pytest.CaptureFixture[str]) -> None:
        preview = dry_run_preview("write", path="/test.txt")
        render_dry_run(preview)
        captured = capsys.readouterr()
        assert "DRY RUN" in captured.out
        assert "/test.txt" in captured.out

    def test_human_mode_with_source_dest(self, capsys: pytest.CaptureFixture[str]) -> None:
        preview = dry_run_preview("cp", source="/a", dest="/b")
        render_dry_run(preview)
        captured = capsys.readouterr()
        assert "/a" in captured.out
        assert "/b" in captured.out


# ---------------------------------------------------------------------------
# Per-command smoke tests — assert --dry-run prevents mutation
# ---------------------------------------------------------------------------


def _make_mock_nx() -> MagicMock:
    """Create a mock NexusFS that tracks calls."""
    nx = MagicMock()
    nx.close = MagicMock()
    nx.access = MagicMock(return_value=True)
    nx.sys_write = MagicMock()
    nx.sys_read = MagicMock()
    nx.sys_unlink = MagicMock()
    nx.sys_rename = MagicMock()
    nx.mkdir = MagicMock()
    nx.rmdir = MagicMock()
    nx.sys_readdir = MagicMock()
    return nx


@contextmanager
def _patch_open_filesystem(nx: MagicMock) -> Any:
    """Patch open_filesystem to yield our mock."""

    @asynccontextmanager
    async def _mock_open(*_args: Any, **_kwargs: Any) -> Any:
        yield nx

    # Patch in all command modules that use open_filesystem
    with (
        patch("nexus.cli.commands.file_ops.open_filesystem", _mock_open),
        patch("nexus.cli.commands.directory.open_filesystem", _mock_open),
    ):
        yield


class TestWriteDryRun:
    def test_dry_run_does_not_write(self) -> None:
        from nexus.cli.commands.file_ops import write

        nx = _make_mock_nx()
        runner = CliRunner()
        with _patch_open_filesystem(nx):
            result = runner.invoke(
                write, ["/test.txt", "hello", "--dry-run"], catch_exceptions=False
            )
        assert result.exit_code == 0
        assert "DRY RUN" in result.output
        nx.sys_write.assert_not_called()


class TestRmDryRun:
    def test_dry_run_does_not_delete(self) -> None:
        from nexus.cli.commands.file_ops import rm

        nx = _make_mock_nx()
        runner = CliRunner()
        with _patch_open_filesystem(nx):
            result = runner.invoke(rm, ["/test.txt", "--dry-run"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "DRY RUN" in result.output
        nx.sys_unlink.assert_not_called()


class TestCpDryRun:
    def test_dry_run_does_not_copy(self) -> None:
        from nexus.cli.commands.file_ops import cp

        nx = _make_mock_nx()
        runner = CliRunner()
        with _patch_open_filesystem(nx):
            result = runner.invoke(cp, ["/a", "/b", "--dry-run"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "DRY RUN" in result.output
        nx.sys_read.assert_not_called()
        nx.sys_write.assert_not_called()


class TestMoveDryRun:
    def test_dry_run_does_not_move(self) -> None:
        from nexus.cli.commands.file_ops import move_cmd

        runner = CliRunner()
        with patch("nexus.cli.commands.file_ops.open_filesystem"):
            result = runner.invoke(move_cmd, ["/a", "/b", "--dry-run"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "DRY RUN" in result.output


class TestMkdirDryRun:
    def test_dry_run_does_not_create(self) -> None:
        from nexus.cli.commands.directory import mkdir

        nx = _make_mock_nx()
        runner = CliRunner()
        with _patch_open_filesystem(nx):
            result = runner.invoke(mkdir, ["/test-dir", "--dry-run"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "DRY RUN" in result.output
        nx.mkdir.assert_not_called()


class TestRmdirDryRun:
    def test_dry_run_does_not_remove(self) -> None:
        from nexus.cli.commands.directory import rmdir

        nx = _make_mock_nx()
        runner = CliRunner()
        with _patch_open_filesystem(nx):
            result = runner.invoke(rmdir, ["/test-dir", "--dry-run"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "DRY RUN" in result.output
        nx.rmdir.assert_not_called()
