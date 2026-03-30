"""Tests for the ``nexus-fs mount`` CLI command."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from nexus.fs._cli import main


class _FakeFS:
    """Minimal stand-in for SlimNexusFS returned by mount()."""

    def __init__(self, mounts: list[str]) -> None:
        self._mounts = mounts

    def list_mounts(self) -> list[str]:
        return self._mounts


def _make_mock_mount(mounts: list[str]) -> AsyncMock:
    return AsyncMock(return_value=_FakeFS(mounts))


def _env_no_auto_json() -> dict[str, str]:
    """Disable auto-JSON so CliRunner (non-TTY) gets human output."""
    return {"NEXUS_NO_AUTO_JSON": "1"}


def test_mount_single_uri() -> None:
    mock_mount = _make_mock_mount(["/s3/my-bucket"])

    with patch("nexus.fs.mount", mock_mount):
        runner = CliRunner(env=_env_no_auto_json())
        result = runner.invoke(main, ["mount", "s3://my-bucket"])

    assert result.exit_code == 0
    assert "/s3/my-bucket" in result.output
    assert "Mounted 1 backend(s)." in result.output
    mock_mount.assert_awaited_once_with("s3://my-bucket", at=None)


def test_mount_multiple_uris() -> None:
    mock_mount = _make_mock_mount(["/gcs/bucket", "/s3/my-bucket"])

    with patch("nexus.fs.mount", mock_mount):
        runner = CliRunner(env=_env_no_auto_json())
        result = runner.invoke(main, ["mount", "s3://my-bucket", "gcs://project/bucket"])

    assert result.exit_code == 0
    assert "Mounted 2 backend(s)." in result.output
    mock_mount.assert_awaited_once_with("s3://my-bucket", "gcs://project/bucket", at=None)


def test_mount_with_at_option() -> None:
    mock_mount = _make_mock_mount(["/custom/path"])

    with patch("nexus.fs.mount", mock_mount):
        runner = CliRunner(env=_env_no_auto_json())
        result = runner.invoke(main, ["mount", "s3://my-bucket", "--at", "/custom/path"])

    assert result.exit_code == 0
    assert "/custom/path" in result.output
    mock_mount.assert_awaited_once_with("s3://my-bucket", at="/custom/path")


def test_mount_json_output() -> None:
    mock_mount = _make_mock_mount(["/s3/my-bucket"])

    with patch("nexus.fs.mount", mock_mount):
        runner = CliRunner()
        result = runner.invoke(main, ["mount", "s3://my-bucket", "--json"])

    assert result.exit_code == 0
    envelope = json.loads(result.output)
    assert envelope["data"]["mounts"] == ["/s3/my-bucket"]
    assert envelope["data"]["uris"] == ["s3://my-bucket"]


def test_mount_no_uris_shows_usage_error() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["mount"])

    assert result.exit_code != 0


def test_mount_error_exits_nonzero() -> None:
    mock_mount = AsyncMock(side_effect=ValueError("Invalid URI"))

    with patch("nexus.fs.mount", mock_mount):
        runner = CliRunner()
        result = runner.invoke(main, ["mount", "bad://uri"])

    assert result.exit_code == 1
    assert "Invalid URI" in result.output
