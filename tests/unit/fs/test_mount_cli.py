"""Tests for the ``nexus-fs mount`` and ``unmount`` CLI commands."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

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


def _mock_create_backend(**kwargs):
    """Return a mock create_backend that produces a stub backend."""
    backend = MagicMock()
    backend.name = "test_backend"
    backend.close = MagicMock()
    return patch("nexus.fs._backend_factory.create_backend", return_value=backend, **kwargs)


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


def test_mount_list_human_output(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
    (tmp_path / "mounts.json").write_text(
        json.dumps(
            [
                {"uri": "s3://my-bucket", "at": "/data"},
                {"uri": "local:///tmp/cache", "at": None},
            ]
        )
    )

    runner = CliRunner(env=_env_no_auto_json())
    result = runner.invoke(main, ["mount", "list"])

    assert result.exit_code == 0
    assert "s3://my-bucket -> /data [persisted]" in result.output
    assert "local:///tmp/cache -> (default) [persisted]" in result.output


def test_mount_list_json_output(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
    (tmp_path / "mounts.json").write_text(json.dumps([{"uri": "s3://my-bucket", "at": "/data"}]))

    runner = CliRunner()
    result = runner.invoke(main, ["mount", "list", "--json"])

    assert result.exit_code == 0
    envelope = json.loads(result.output)
    assert envelope["data"]["mounts"] == [
        {"uri": "s3://my-bucket", "at": "/data", "status": "persisted"}
    ]


def test_mount_list_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))

    runner = CliRunner(env=_env_no_auto_json())
    result = runner.invoke(main, ["mount", "list"])

    assert result.exit_code == 0
    assert "No persisted mounts." in result.output


def test_mount_test_runs_doctor_without_persisting(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))

    mock_mount = _make_mock_mount(["/s3/my-bucket"])

    with (
        patch("nexus.fs.mount", mock_mount),
        patch(
            "nexus.fs._doctor.run_all_checks",
            AsyncMock(
                return_value={
                    "Environment": [],
                    "Backends": [],
                    "Mounts": [],
                }
            ),
        ),
    ):
        runner = CliRunner()
        result = runner.invoke(main, ["mount", "test", "s3://my-bucket", "--json"])

    assert result.exit_code == 0
    envelope = json.loads(result.output)
    assert "Mounts" in envelope["data"]
    assert not (tmp_path / "mounts.json").exists()
    mock_mount.assert_awaited_once_with("s3://my-bucket")


def test_mount_test_restores_existing_mounts_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
    original = [{"uri": "local:///tmp/cache", "at": None}]
    mounts_path = tmp_path / "mounts.json"
    mounts_path.write_text(json.dumps(original))

    mock_mount = _make_mock_mount(["/s3/my-bucket"])

    with (
        patch("nexus.fs.mount", mock_mount),
        patch(
            "nexus.fs._doctor.run_all_checks",
            AsyncMock(
                return_value={
                    "Environment": [],
                    "Backends": [],
                    "Mounts": [],
                }
            ),
        ),
    ):
        runner = CliRunner(env=_env_no_auto_json())
        result = runner.invoke(main, ["mount", "test", "s3://my-bucket"])

    assert result.exit_code == 0
    assert json.loads(mounts_path.read_text()) == original


def test_unmount_removes_entry(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
    mounts_path = tmp_path / "mounts.json"
    mounts_path.write_text(
        json.dumps(
            [
                {"uri": "s3://my-bucket", "at": "/data"},
                {"uri": "local:///tmp/cache", "at": None},
            ]
        )
    )

    runner = CliRunner(env=_env_no_auto_json())
    result = runner.invoke(main, ["unmount", "s3://my-bucket"])

    assert result.exit_code == 0
    assert "Removed persisted mount: s3://my-bucket" in result.output
    assert json.loads(mounts_path.read_text()) == [{"uri": "local:///tmp/cache", "at": None}]


def test_unmount_missing_uri_exits_nonzero(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
    (tmp_path / "mounts.json").write_text(json.dumps([{"uri": "local:///tmp/cache", "at": None}]))

    runner = CliRunner()
    result = runner.invoke(main, ["unmount", "s3://my-bucket"])

    assert result.exit_code == 1
    assert "mount not found" in result.output.lower()


# =========================================================================
# Persistence: --at is persisted and repeated mounts merge
# =========================================================================


class TestMountPersistence:
    """Verify mounts.json persists --at and merges across invocations.

    These tests mock at the create_backend level so that mount() runs its
    real persistence logic (save_persisted_mounts).
    """

    def test_at_persisted_to_mounts_json(self, tmp_path, monkeypatch) -> None:
        """--at value must appear in mounts.json so later commands can restore it."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))

        with _mock_create_backend():
            runner = CliRunner(env=_env_no_auto_json())
            result = runner.invoke(main, ["mount", "local:///tmp/data", "--at", "/custom"])

        assert result.exit_code == 0

        mounts_data = json.loads((tmp_path / "mounts.json").read_text())
        assert len(mounts_data) == 1
        assert mounts_data[0]["uri"] == "local:///tmp/data"
        assert mounts_data[0]["at"] == "/custom"

    def test_repeated_mounts_merge(self, tmp_path, monkeypatch) -> None:
        """Second mount invocation must add to existing entries, not overwrite."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))

        with _mock_create_backend():
            runner = CliRunner(env=_env_no_auto_json())
            runner.invoke(main, ["mount", "local:///tmp/a"])
            runner.invoke(main, ["mount", "local:///tmp/b"])

        mounts_data = json.loads((tmp_path / "mounts.json").read_text())
        uris = [e["uri"] for e in mounts_data]
        assert "local:///tmp/a" in uris
        assert "local:///tmp/b" in uris
        assert len(mounts_data) == 2

    def test_repeated_mount_same_uri_deduplicates(self, tmp_path, monkeypatch) -> None:
        """Mounting the same URI twice should not create a duplicate entry."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))

        with _mock_create_backend():
            runner = CliRunner(env=_env_no_auto_json())
            runner.invoke(main, ["mount", "local:///tmp/data"])
            runner.invoke(main, ["mount", "local:///tmp/data"])

        mounts_data = json.loads((tmp_path / "mounts.json").read_text())
        assert len(mounts_data) == 1

    def test_mount_overrides_persisted_via_python_api(self, tmp_path, monkeypatch) -> None:
        """mount(mount_overrides=...) must persist per-URI at values."""
        import asyncio

        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))

        with _mock_create_backend():
            from nexus.fs import mount

            asyncio.run(
                mount(
                    "local:///tmp/a",
                    "local:///tmp/b",
                    mount_overrides={"local:///tmp/a": "/data"},
                )
            )

        mounts_data = json.loads((tmp_path / "mounts.json").read_text())
        by_uri = {e["uri"]: e["at"] for e in mounts_data}
        assert by_uri["local:///tmp/a"] == "/data"
        assert by_uri["local:///tmp/b"] is None

    def test_at_restored_by_cp_single_mount(self, tmp_path, monkeypatch) -> None:
        """cp should restore --at from persisted mounts.json (single mount)."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))

        (tmp_path / "mounts.json").write_text(
            json.dumps([{"uri": "s3://my-bucket", "at": "/data"}])
        )

        mock_mount = _make_mock_mount(["/data"])
        mock_mount.return_value.copy = AsyncMock(return_value={"size": 10})

        with patch("nexus.fs.mount", mock_mount):
            runner = CliRunner(env=_env_no_auto_json())
            runner.invoke(main, ["cp", "/data/a.txt", "/data/b.txt"])

        mock_mount.assert_awaited_once_with(
            "s3://my-bucket",
            mount_overrides={"s3://my-bucket": "/data"},
            skip_unavailable=True,
        )

    def test_at_restored_by_cp_multiple_mounts(self, tmp_path, monkeypatch) -> None:
        """cp should restore --at even when there are multiple persisted mounts."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))

        (tmp_path / "mounts.json").write_text(
            json.dumps(
                [
                    {"uri": "s3://my-bucket", "at": "/data"},
                    {"uri": "local:///tmp/b", "at": None},
                ]
            )
        )

        mock_mount = _make_mock_mount(["/data", "/local/b"])
        mock_mount.return_value.copy = AsyncMock(return_value={"size": 10})

        with patch("nexus.fs.mount", mock_mount):
            runner = CliRunner(env=_env_no_auto_json())
            runner.invoke(main, ["cp", "/data/a.txt", "/local/b/a.txt"])

        mock_mount.assert_awaited_once_with(
            "s3://my-bucket",
            "local:///tmp/b",
            mount_overrides={"s3://my-bucket": "/data"},
            skip_unavailable=True,
        )

    def test_backward_compat_legacy_format(self, tmp_path, monkeypatch) -> None:
        """Legacy mounts.json with plain URI strings should still work."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))

        (tmp_path / "mounts.json").write_text(json.dumps(["s3://old-bucket"]))

        mock_mount = _make_mock_mount(["/s3/old-bucket"])
        mock_mount.return_value.copy = AsyncMock(return_value={"size": 5})

        with patch("nexus.fs.mount", mock_mount):
            runner = CliRunner(env=_env_no_auto_json())
            runner.invoke(main, ["cp", "/s3/old-bucket/a", "/s3/old-bucket/b"])

        mock_mount.assert_awaited_once_with(
            "s3://old-bucket",
            mount_overrides=None,
            skip_unavailable=True,
        )
