"""TDD tests for ``nexus-fs mount prune``.

Written before the implementation as a behavioural spec.  Every test here
describes a contract that the prune command must satisfy.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from nexus.fs._cli import main

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env_no_auto_json() -> dict[str, str]:
    return {"NEXUS_NO_AUTO_JSON": "1"}


def _write_mounts(tmp_path: Path, entries: list[dict]) -> Path:
    mf = tmp_path / "mounts.json"
    mf.write_text(json.dumps(entries))
    return mf


def _local_path(tmp_path: Path, name: str, *, create: bool = True) -> str:
    """Return a local:// URI for a subdir; optionally create the dir."""
    p = tmp_path / name
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return f"local://{p}"


# ---------------------------------------------------------------------------
# 1. --dry-run: never modifies mounts.json
# ---------------------------------------------------------------------------


class TestPruneDryRun:
    def test_dry_run_does_not_modify_mounts_json(self, tmp_path, monkeypatch) -> None:
        """--dry-run must leave mounts.json byte-for-byte identical."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        entries = [
            {"uri": _local_path(tmp_path, "real", create=True), "at": None},
            {"uri": _local_path(tmp_path, "gone", create=False), "at": None},
        ]
        mf = _write_mounts(tmp_path, entries)
        before = mf.read_text()

        runner = CliRunner(env=_env_no_auto_json())
        result = runner.invoke(main, ["mount", "prune", "--stale", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert mf.read_text() == before, "dry-run must not modify mounts.json"

    def test_dry_run_reports_what_would_be_removed(self, tmp_path, monkeypatch) -> None:
        """--dry-run output must name the stale URIs it would remove."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        stale_uri = _local_path(tmp_path, "gone", create=False)
        _write_mounts(tmp_path, [{"uri": stale_uri, "at": None}])

        runner = CliRunner(env=_env_no_auto_json())
        result = runner.invoke(main, ["mount", "prune", "--stale", "--dry-run"])

        assert result.exit_code == 0
        assert stale_uri in result.output

    def test_dry_run_does_not_write_backup(self, tmp_path, monkeypatch) -> None:
        """--dry-run must not create any .bak file."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        _write_mounts(tmp_path, [{"uri": _local_path(tmp_path, "gone", create=False), "at": None}])

        runner = CliRunner(env=_env_no_auto_json())
        runner.invoke(main, ["mount", "prune", "--stale", "--dry-run"])

        assert not list(tmp_path.glob("mounts.json.bak*")), "dry-run must not create backups"


# ---------------------------------------------------------------------------
# 2. --stale: removes only entries whose local:// path doesn't exist
# ---------------------------------------------------------------------------


class TestPruneStale:
    def test_prune_stale_removes_only_dead_local_paths(self, tmp_path, monkeypatch) -> None:
        """--stale removes entries whose local:// path is gone; leaves live ones."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        live_uri = _local_path(tmp_path, "live", create=True)
        dead_uri = _local_path(tmp_path, "dead", create=False)
        _write_mounts(
            tmp_path,
            [
                {"uri": live_uri, "at": None},
                {"uri": dead_uri, "at": None},
            ],
        )

        runner = CliRunner(env=_env_no_auto_json())
        result = runner.invoke(main, ["mount", "prune", "--stale", "--yes"])

        assert result.exit_code == 0, result.output
        remaining = json.loads((tmp_path / "mounts.json").read_text())
        uris = [e["uri"] for e in remaining]
        assert live_uri in uris
        assert dead_uri not in uris

    def test_prune_stale_non_local_uris_treated_as_live(self, tmp_path, monkeypatch) -> None:
        """Non-local:// URIs are not checked for staleness and are kept."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        cloud_uri = "s3://my-real-bucket"
        dead_local = _local_path(tmp_path, "gone", create=False)
        _write_mounts(
            tmp_path,
            [
                {"uri": cloud_uri, "at": None},
                {"uri": dead_local, "at": None},
            ],
        )

        runner = CliRunner(env=_env_no_auto_json())
        result = runner.invoke(main, ["mount", "prune", "--stale", "--yes"])

        assert result.exit_code == 0
        remaining = json.loads((tmp_path / "mounts.json").read_text())
        uris = [e["uri"] for e in remaining]
        assert cloud_uri in uris
        assert dead_local not in uris

    def test_prune_stale_all_live_is_noop(self, tmp_path, monkeypatch) -> None:
        """When all entries are live, prune --stale makes no changes."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        live_uri = _local_path(tmp_path, "live", create=True)
        mf = _write_mounts(tmp_path, [{"uri": live_uri, "at": None}])
        before = mf.read_text()

        runner = CliRunner(env=_env_no_auto_json())
        result = runner.invoke(main, ["mount", "prune", "--stale", "--yes"])

        assert result.exit_code == 0
        assert mf.read_text() == before

    def test_prune_stale_all_live_does_not_write_backup(self, tmp_path, monkeypatch) -> None:
        """No backup when nothing is pruned."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        _write_mounts(tmp_path, [{"uri": _local_path(tmp_path, "live", create=True), "at": None}])

        runner = CliRunner(env=_env_no_auto_json())
        runner.invoke(main, ["mount", "prune", "--stale", "--yes"])

        assert not list(tmp_path.glob("mounts.json.bak*"))


# ---------------------------------------------------------------------------
# 3. --filter glob matching
# ---------------------------------------------------------------------------


class TestPruneFilter:
    def test_filter_glob_removes_matching_entries(self, tmp_path, monkeypatch) -> None:
        """--filter 'local:///tmp/*test*' removes entries matching the glob."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        test_uri = "local:///tmp/koi-fs-nexus-test-abc123"
        real_uri = "gws://calendar"
        _write_mounts(
            tmp_path,
            [
                {"uri": test_uri, "at": None},
                {"uri": real_uri, "at": None},
            ],
        )

        runner = CliRunner(env=_env_no_auto_json())
        result = runner.invoke(main, ["mount", "prune", "--filter", "local:///tmp/*test*", "--yes"])

        assert result.exit_code == 0, result.output
        remaining = json.loads((tmp_path / "mounts.json").read_text())
        uris = [e["uri"] for e in remaining]
        assert test_uri not in uris
        assert real_uri in uris

    def test_filter_no_match_is_noop(self, tmp_path, monkeypatch) -> None:
        """--filter with no matching entries makes no changes."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        mf = _write_mounts(tmp_path, [{"uri": "s3://my-bucket", "at": None}])
        before = mf.read_text()

        runner = CliRunner(env=_env_no_auto_json())
        result = runner.invoke(main, ["mount", "prune", "--filter", "local:///tmp/*test*", "--yes"])

        assert result.exit_code == 0
        assert mf.read_text() == before


# ---------------------------------------------------------------------------
# 4. Backup creation and rotation (timestamped, keep N=3)
# ---------------------------------------------------------------------------


class TestPruneBackup:
    def test_backup_written_before_modification(self, tmp_path, monkeypatch) -> None:
        """A .bak backup is created before any prune that modifies mounts.json."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        dead_uri = _local_path(tmp_path, "gone", create=False)
        original_content = json.dumps([{"uri": dead_uri, "at": None}])
        (tmp_path / "mounts.json").write_text(original_content)

        runner = CliRunner(env=_env_no_auto_json())
        runner.invoke(main, ["mount", "prune", "--stale", "--yes"])

        bak_files = list(tmp_path.glob("mounts.json.bak.*"))
        assert len(bak_files) == 1, f"expected one .bak, found: {bak_files}"
        assert bak_files[0].read_text() == original_content

    def test_backup_rotation_keeps_at_most_three(self, tmp_path, monkeypatch) -> None:
        """After 4 prune runs, at most 3 .bak files survive."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))

        runner = CliRunner(env=_env_no_auto_json())

        for i in range(4):
            dead = _local_path(tmp_path, f"gone_{i}", create=False)
            _write_mounts(tmp_path, [{"uri": dead, "at": None}])
            runner.invoke(main, ["mount", "prune", "--stale", "--yes"])

        bak_files = list(tmp_path.glob("mounts.json.bak.*"))
        assert len(bak_files) <= 3, f"expected ≤3 backups, found {len(bak_files)}: {bak_files}"


# ---------------------------------------------------------------------------
# 5. Confirmation prompt: user says "n" → no changes
# ---------------------------------------------------------------------------


class TestPruneConfirmation:
    def test_declining_confirmation_makes_no_changes(self, tmp_path, monkeypatch) -> None:
        """If the user declines the confirmation prompt, mounts.json is untouched."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        dead_uri = _local_path(tmp_path, "gone", create=False)
        mf = _write_mounts(tmp_path, [{"uri": dead_uri, "at": None}])
        before = mf.read_text()

        runner = CliRunner(env=_env_no_auto_json())
        # Simulate user typing "n" at the confirmation prompt
        runner.invoke(main, ["mount", "prune", "--stale"], input="n\n")

        assert mf.read_text() == before

    def test_yes_flag_skips_confirmation(self, tmp_path, monkeypatch) -> None:
        """--yes skips the interactive prompt."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        dead_uri = _local_path(tmp_path, "gone", create=False)
        _write_mounts(tmp_path, [{"uri": dead_uri, "at": None}])

        runner = CliRunner(env=_env_no_auto_json())
        result = runner.invoke(main, ["mount", "prune", "--stale", "--yes"])

        assert result.exit_code == 0
        remaining = json.loads((tmp_path / "mounts.json").read_text())
        assert remaining == []


# ---------------------------------------------------------------------------
# 6. Edge cases: empty registry, pruning all entries
# ---------------------------------------------------------------------------


class TestPruneEdgeCases:
    def test_prune_empty_registry_is_noop(self, tmp_path, monkeypatch) -> None:
        """Pruning an empty mounts.json exits cleanly with no changes."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        mf = _write_mounts(tmp_path, [])

        runner = CliRunner(env=_env_no_auto_json())
        result = runner.invoke(main, ["mount", "prune", "--stale", "--yes"])

        assert result.exit_code == 0
        assert json.loads(mf.read_text()) == []
        assert not list(tmp_path.glob("mounts.json.bak*"))

    def test_prune_all_entries_leaves_empty_file_not_deleted(self, tmp_path, monkeypatch) -> None:
        """Pruning all entries writes [] to mounts.json; it must not be deleted."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))
        dead_uri = _local_path(tmp_path, "gone", create=False)
        mf = _write_mounts(tmp_path, [{"uri": dead_uri, "at": None}])

        runner = CliRunner(env=_env_no_auto_json())
        runner.invoke(main, ["mount", "prune", "--stale", "--yes"])

        assert mf.exists(), "mounts.json must not be deleted, only emptied"
        assert json.loads(mf.read_text()) == []

    def test_prune_missing_mounts_json_exits_cleanly(self, tmp_path, monkeypatch) -> None:
        """If mounts.json doesn't exist, prune exits cleanly with a message."""
        monkeypatch.setenv("NEXUS_FS_STATE_DIR", str(tmp_path))

        runner = CliRunner(env=_env_no_auto_json())
        result = runner.invoke(main, ["mount", "prune", "--stale", "--yes"])

        assert result.exit_code == 0
