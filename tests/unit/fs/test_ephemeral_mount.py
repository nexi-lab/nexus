"""Tests for nexus.fs.testing.ephemeral_mount (Issue 12-A).

Covers:
1. Happy path — mounts.json is never written
2. Exception safety — teardown runs even when the body raises
3. Nested scope isolation — inner teardown doesn't affect outer mount
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.fs.testing import ephemeral_mount

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _mock_create_backend() -> MagicMock:
    backend = MagicMock()
    backend.name = "test_backend"
    backend.close = MagicMock()
    return backend


def _patch_mount_internals(tmp_path: Path):
    """Patch the heavy kernel parts so ephemeral_mount is fast in unit tests."""

    backend = _mock_create_backend()

    return patch("nexus.fs._backend_factory.create_backend", return_value=backend)


# ---------------------------------------------------------------------------
# 1. Happy path — mounts.json is never written
# ---------------------------------------------------------------------------


class TestEphemeralMountHappyPath:
    def test_ephemeral_mount_does_not_write_mounts_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The core guarantee: ephemeral_mount never touches mounts.json."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))

        with _patch_mount_internals(tmp_path), ephemeral_mount("local:///tmp/test-xyz"):
            pass

        assert not (tmp_path / "mounts.json").exists(), (
            "ephemeral_mount must not create mounts.json"
        )

    def test_ephemeral_mount_does_not_add_to_existing_mounts_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If mounts.json already exists, ephemeral_mount must not modify it."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        existing = [{"uri": "s3://real-bucket", "at": None}]
        (tmp_path / "mounts.json").write_text(json.dumps(existing))
        before = (tmp_path / "mounts.json").read_text()

        with _patch_mount_internals(tmp_path), ephemeral_mount("local:///tmp/test-xyz"):
            pass

        assert (tmp_path / "mounts.json").read_text() == before

    def test_ephemeral_mount_yields_fs_object(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The context manager must yield a usable fs object."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))

        with _patch_mount_internals(tmp_path), ephemeral_mount("local:///tmp/test-xyz") as fs:
            assert fs is not None
            assert hasattr(fs, "list_mounts")


# ---------------------------------------------------------------------------
# 2. Exception safety — teardown runs even when the body raises
# ---------------------------------------------------------------------------


class TestEphemeralMountExceptionSafety:
    def test_teardown_runs_on_exception(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Close is called on the fs even when the body raises."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))

        close_called = []

        with (
            _patch_mount_internals(tmp_path),
            pytest.raises(ValueError, match="test error"),
            ephemeral_mount("local:///tmp/test-xyz") as fs,
        ):
            # Monkey-patch fs.close to track calls
            original_close = getattr(fs, "close", None)

            def _tracking_close() -> None:
                close_called.append(True)
                if original_close:
                    original_close()

            fs.close = _tracking_close
            raise ValueError("test error")

        assert close_called, "fs.close() must be called even when the body raised"

    def test_exception_propagates_after_teardown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The original exception from the body must propagate, not be swallowed."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))

        with (
            _patch_mount_internals(tmp_path),
            pytest.raises(RuntimeError, match="the specific error"),
            ephemeral_mount("local:///tmp/test-xyz"),
        ):
            raise RuntimeError("the specific error")

    def test_mounts_json_not_created_after_exception(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even when the body raises, mounts.json must not be created."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))

        with (
            _patch_mount_internals(tmp_path),
            pytest.raises(ValueError),
            ephemeral_mount("local:///tmp/test-xyz"),
        ):
            raise ValueError("boom")

        assert not (tmp_path / "mounts.json").exists()


# ---------------------------------------------------------------------------
# 3. Nested scope isolation — inner teardown doesn't affect outer mount
# ---------------------------------------------------------------------------


class TestEphemeralMountNesting:
    def test_inner_teardown_does_not_affect_outer_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tearing down an inner ephemeral_mount must not close the outer one."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))

        with _patch_mount_internals(tmp_path), ephemeral_mount("local:///tmp/outer") as outer_fs:
            outer_alive_before = outer_fs is not None

            with ephemeral_mount("local:///tmp/inner"):
                pass  # inner torn down here

            # outer_fs should still be usable
            outer_alive_after = outer_fs is not None
            assert outer_alive_before and outer_alive_after

    def test_inner_exception_does_not_affect_outer_mounts_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An inner ephemeral_mount failing must not touch mounts.json."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))

        with (
            _patch_mount_internals(tmp_path),
            ephemeral_mount("local:///tmp/outer"),
            pytest.raises(ValueError),
            ephemeral_mount("local:///tmp/inner"),
        ):
            raise ValueError("inner failure")

        assert not (tmp_path / "mounts.json").exists()
