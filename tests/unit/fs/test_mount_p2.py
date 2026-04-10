"""Tests for P2 features: metadata, named mounts, --older-than, mount rm.

Separate file to keep test_mount_cli.py focused on the core CLI surface.
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

from click.testing import CliRunner

from nexus.fs._cli import main
from nexus.fs._paths import load_persisted_mounts, save_persisted_mounts

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env() -> dict[str, str]:
    return {"NEXUS_NO_AUTO_JSON": "1"}


def _mock_create_backend():
    from unittest.mock import MagicMock

    backend = MagicMock()
    backend.name = "test_backend"
    backend.close = MagicMock()
    return patch("nexus.fs._backend_factory.create_backend", return_value=backend)


# ---------------------------------------------------------------------------
# Entry metadata: created_at / created_by / last_used_at
# ---------------------------------------------------------------------------


class TestEntryMetadata:
    def test_new_entry_gets_created_at(self, tmp_path, monkeypatch):
        """Newly saved entries must have created_at stamped."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        save_persisted_mounts([{"uri": "s3://bucket", "at": None}])

        entries = load_persisted_mounts()
        assert len(entries) == 1
        assert entries[0]["created_at"] is not None

    def test_new_entry_gets_created_by(self, tmp_path, monkeypatch):
        """Newly saved entries must have created_by with pid and exe."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        save_persisted_mounts([{"uri": "s3://bucket", "at": None}])

        entries = load_persisted_mounts()
        assert "pid=" in entries[0]["created_by"]
        assert "exe=" in entries[0]["created_by"]

    def test_new_entry_gets_last_used_at(self, tmp_path, monkeypatch):
        """last_used_at is set to now on first save."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        save_persisted_mounts([{"uri": "s3://bucket", "at": None}])

        entries = load_persisted_mounts()
        assert entries[0]["last_used_at"] is not None

    def test_remount_preserves_created_at(self, tmp_path, monkeypatch):
        """Re-saving an existing URI must not overwrite created_at."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        save_persisted_mounts([{"uri": "s3://bucket", "at": None}])
        original_created = load_persisted_mounts()[0]["created_at"]

        # Small delay to ensure timestamps differ if incorrectly overwritten
        time.sleep(0.01)
        save_persisted_mounts([{"uri": "s3://bucket", "at": None}])

        entries = load_persisted_mounts()
        assert entries[0]["created_at"] == original_created

    def test_remount_updates_last_used_at(self, tmp_path, monkeypatch):
        """Re-saving an existing URI must update last_used_at."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        save_persisted_mounts([{"uri": "s3://bucket", "at": None}])
        first_used = load_persisted_mounts()[0]["last_used_at"]

        time.sleep(0.01)
        save_persisted_mounts([{"uri": "s3://bucket", "at": None}])

        entries = load_persisted_mounts()
        assert entries[0]["last_used_at"] >= first_used

    def test_legacy_entry_without_metadata_loads_cleanly(self, tmp_path, monkeypatch):
        """Legacy entries (no metadata fields) round-trip without error."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        (tmp_path / "mounts.json").write_text(json.dumps([{"uri": "s3://old-bucket", "at": None}]))

        entries = load_persisted_mounts()
        assert entries[0]["uri"] == "s3://old-bucket"
        assert entries[0]["created_at"] is None  # not present in legacy entry


# ---------------------------------------------------------------------------
# Named mounts: mount add NAME URI, mount rm NAME
# ---------------------------------------------------------------------------


class TestNamedMounts:
    def test_mount_add_with_name_stores_name(self, tmp_path, monkeypatch):
        """mount add gmail gws://gmail stores name='gmail' in mounts.json."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))

        with _mock_create_backend():
            runner = CliRunner(env=_env())
            result = runner.invoke(main, ["mount", "add", "gmail", "local:///tmp/inbox"])

        assert result.exit_code == 0, result.output
        entries = json.loads((tmp_path / "mounts.json").read_text())
        assert any(e.get("name") == "gmail" for e in entries)

    def test_mount_add_name_shown_in_output(self, tmp_path, monkeypatch):
        """mount add gmail ... mentions the name in human output."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))

        with _mock_create_backend():
            runner = CliRunner(env=_env())
            result = runner.invoke(main, ["mount", "add", "gmail", "local:///tmp/inbox"])

        assert "gmail" in result.output

    def test_mount_add_name_requires_single_uri(self, tmp_path, monkeypatch):
        """A name cannot be combined with multiple URIs."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        runner = CliRunner(env=_env())
        result = runner.invoke(main, ["mount", "add", "myname", "local:///tmp/a", "local:///tmp/b"])
        assert result.exit_code != 0

    def test_mount_rm_by_name(self, tmp_path, monkeypatch):
        """mount rm gmail removes the entry named 'gmail'."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        save_persisted_mounts(
            [
                {"uri": "gws://gmail", "at": None, "name": "gmail"},
                {"uri": "s3://keep", "at": None, "name": None},
            ],
            merge=False,
        )

        runner = CliRunner(env=_env())
        result = runner.invoke(main, ["mount", "rm", "gmail"])

        assert result.exit_code == 0, result.output
        remaining = json.loads((tmp_path / "mounts.json").read_text())
        uris = [e["uri"] for e in remaining]
        assert "gws://gmail" not in uris
        assert "s3://keep" in uris

    def test_mount_rm_by_uri(self, tmp_path, monkeypatch):
        """mount rm gws://gmail removes the entry by URI."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        save_persisted_mounts(
            [
                {"uri": "gws://gmail", "at": None, "name": "gmail"},
            ],
            merge=False,
        )

        runner = CliRunner(env=_env())
        result = runner.invoke(main, ["mount", "rm", "gws://gmail"])

        assert result.exit_code == 0
        remaining = json.loads((tmp_path / "mounts.json").read_text())
        assert remaining == []

    def test_mount_rm_unknown_exits_nonzero(self, tmp_path, monkeypatch):
        """mount rm with no match exits with a non-zero code."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        save_persisted_mounts([{"uri": "s3://bucket", "at": None}], merge=False)

        runner = CliRunner(env=_env())
        result = runner.invoke(main, ["mount", "rm", "nonexistent"])

        assert result.exit_code != 0

    def test_mount_list_shows_name(self, tmp_path, monkeypatch):
        """mount list displays the @name suffix when a name is present."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        (tmp_path / "mounts.json").write_text(
            json.dumps([{"uri": "s3://bucket", "at": None, "name": "work"}])
        )

        runner = CliRunner(env=_env())
        result = runner.invoke(main, ["mount", "list"])

        assert result.exit_code == 0
        assert "@work" in result.output


# ---------------------------------------------------------------------------
# prune --older-than
# ---------------------------------------------------------------------------


class TestPruneOlderThan:
    def _write_entry_with_age(self, tmp_path, uri, days_old):
        """Write a mounts.json entry backdated by days_old days."""
        import datetime

        ago = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days_old)
        entries = [
            {
                "uri": uri,
                "at": None,
                "name": None,
                "created_at": ago.isoformat(),
                "created_by": "test",
                "last_used_at": ago.isoformat(),
            }
        ]
        (tmp_path / "mounts.json").write_text(json.dumps(entries))

    def test_older_than_removes_old_entries(self, tmp_path, monkeypatch):
        """--older-than 30d removes entries created more than 30 days ago."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        self._write_entry_with_age(tmp_path, "s3://old-bucket", days_old=60)

        runner = CliRunner(env=_env())
        result = runner.invoke(main, ["mount", "prune", "--older-than", "30d", "--yes"])

        assert result.exit_code == 0, result.output
        remaining = json.loads((tmp_path / "mounts.json").read_text())
        assert remaining == []

    def test_older_than_keeps_recent_entries(self, tmp_path, monkeypatch):
        """--older-than 30d keeps entries created less than 30 days ago."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        self._write_entry_with_age(tmp_path, "s3://new-bucket", days_old=5)

        runner = CliRunner(env=_env())
        result = runner.invoke(main, ["mount", "prune", "--older-than", "30d", "--yes"])

        assert result.exit_code == 0
        # "No entries matched" message expected
        assert "nothing to prune" in result.output.lower() or "no entries" in result.output.lower()
        remaining = json.loads((tmp_path / "mounts.json").read_text())
        assert len(remaining) == 1

    def test_older_than_skips_entries_without_created_at(self, tmp_path, monkeypatch):
        """Entries without created_at are conservatively kept by --older-than."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        (tmp_path / "mounts.json").write_text(
            json.dumps([{"uri": "s3://mystery", "at": None, "created_at": None}])
        )

        runner = CliRunner(env=_env())
        result = runner.invoke(main, ["mount", "prune", "--older-than", "1d", "--yes"])

        assert result.exit_code == 0
        remaining = json.loads((tmp_path / "mounts.json").read_text())
        assert len(remaining) == 1, "entry without created_at must not be removed"

    def test_older_than_invalid_duration_exits_nonzero(self, tmp_path, monkeypatch):
        """Invalid duration string exits with a usage error."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        runner = CliRunner(env=_env())
        result = runner.invoke(main, ["mount", "prune", "--older-than", "not-a-duration", "--yes"])
        assert result.exit_code != 0

    def test_older_than_hours_unit(self, tmp_path, monkeypatch):
        """--older-than accepts hours unit (e.g. '48h')."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        self._write_entry_with_age(tmp_path, "s3://old", days_old=3)  # 72h old

        runner = CliRunner(env=_env())
        result = runner.invoke(main, ["mount", "prune", "--older-than", "48h", "--yes"])

        assert result.exit_code == 0
        remaining = json.loads((tmp_path / "mounts.json").read_text())
        assert remaining == []
