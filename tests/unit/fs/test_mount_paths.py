"""Tests for nexus.fs._paths persistence helpers.

Covers Issue 6-A (corrupt mounts.json warning) and dedup edge cases.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from nexus.fs._paths import load_persisted_mounts, save_persisted_mounts

# ---------------------------------------------------------------------------
# Issue 11-A: load_persisted_mounts error handling
# ---------------------------------------------------------------------------


class TestLoadPersistedMountsErrorHandling:
    def test_missing_file_returns_empty_list_silently(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing mounts.json is not an error — return [] without printing."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))

        captured = io.StringIO()
        monkeypatch.setattr(sys, "stderr", captured)

        result = load_persisted_mounts()

        assert result == []
        assert captured.getvalue() == "", "missing file must not produce stderr output"

    def test_corrupt_json_returns_empty_list_with_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Corrupt mounts.json emits a warning to stderr and returns []."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        (tmp_path / "mounts.json").write_text("{this is not valid json")

        captured = io.StringIO()
        monkeypatch.setattr(sys, "stderr", captured)

        result = load_persisted_mounts()

        assert result == []
        warning = captured.getvalue()
        assert "warning" in warning.lower(), f"expected warning in stderr, got: {warning!r}"
        assert "corrupt" in warning.lower() or "mounts.json" in warning

    def test_corrupt_json_warning_includes_file_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Warning message should name the corrupt file so user can restore from .bak."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        (tmp_path / "mounts.json").write_text("!!!invalid!!!")

        captured = io.StringIO()
        monkeypatch.setattr(sys, "stderr", captured)

        load_persisted_mounts()

        # File path should appear in the warning so user knows which file to restore
        assert "mounts.json" in captured.getvalue()

    def test_valid_json_non_list_returns_empty_list_silently(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Valid JSON but wrong shape ({} instead of []) is silently treated as empty."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        (tmp_path / "mounts.json").write_text(json.dumps({"uri": "s3://bucket"}))

        captured = io.StringIO()
        monkeypatch.setattr(sys, "stderr", captured)

        result = load_persisted_mounts()

        assert result == []
        # This is an unusual but not user-facing-corrupt case — no warning expected
        assert captured.getvalue() == ""


# ---------------------------------------------------------------------------
# save_persisted_mounts: atomic write and overwrite-warning behaviour
# ---------------------------------------------------------------------------


class TestSavePersistedMountsWarning:
    def test_overwrite_same_at_no_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Remounting same URI at same --at produces no warning."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        save_persisted_mounts([{"uri": "s3://my-bucket", "at": "/data"}])

        captured = io.StringIO()
        monkeypatch.setattr(sys, "stderr", captured)

        save_persisted_mounts([{"uri": "s3://my-bucket", "at": "/data"}])

        assert captured.getvalue() == ""

    def test_overwrite_different_at_emits_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Remounting same URI at a different --at emits a warning to stderr."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        save_persisted_mounts([{"uri": "s3://my-bucket", "at": "/fast"}])

        captured = io.StringIO()
        monkeypatch.setattr(sys, "stderr", captured)

        save_persisted_mounts([{"uri": "s3://my-bucket", "at": "/slow"}])

        warning = captured.getvalue()
        assert "warning" in warning.lower()
        assert "s3://my-bucket" in warning

    def test_new_uri_no_warning(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Adding a brand-new URI never emits a warning."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))

        captured = io.StringIO()
        monkeypatch.setattr(sys, "stderr", captured)

        save_persisted_mounts([{"uri": "s3://new-bucket", "at": None}])

        assert captured.getvalue() == ""
