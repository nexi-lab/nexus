"""Tests for nexus.cli.commands.demo — demo init/reset."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from nexus.cli.commands.demo import (
    DEMO_AGENTS,
    DEMO_FILES,
    DEMO_USERS,
    MANIFEST_FILENAME,
    PLAN_VERSIONS,
    _load_manifest,
    _save_manifest,
)

# ---------------------------------------------------------------------------
# Manifest persistence
# ---------------------------------------------------------------------------


class TestManifest:
    def test_load_empty(self, tmp_path: Path) -> None:
        manifest = _load_manifest(str(tmp_path))
        assert manifest == {}

    def test_save_and_load(self, tmp_path: Path) -> None:
        data = {"files": ["/a.txt", "/b.txt"], "seeded_at": "2026-03-12T00:00:00Z"}
        _save_manifest(str(tmp_path), data)
        loaded = _load_manifest(str(tmp_path))
        assert loaded == data

    def test_manifest_path(self, tmp_path: Path) -> None:
        _save_manifest(str(tmp_path), {"test": True})
        assert (tmp_path / MANIFEST_FILENAME).exists()


# ---------------------------------------------------------------------------
# Demo data constants
# ---------------------------------------------------------------------------


class TestDemoConstants:
    def test_demo_files_non_empty(self) -> None:
        assert len(DEMO_FILES) >= 8

    def test_all_files_have_three_fields(self) -> None:
        for path, content, description in DEMO_FILES:
            assert path.startswith("/")
            assert len(content) > 0
            assert len(description) > 0

    def test_plan_versions_non_empty(self) -> None:
        assert len(PLAN_VERSIONS) >= 3

    def test_demo_users(self) -> None:
        user_ids = [u["id"] for u in DEMO_USERS]
        assert "admin" in user_ids
        assert "demo_user" in user_ids

    def test_demo_agents(self) -> None:
        agent_ids = [a["id"] for a in DEMO_AGENTS]
        assert "demo_agent" in agent_ids

    def test_grep_friendly_content(self) -> None:
        """At least one file should contain 'vector index' for grep demos."""
        all_content = " ".join(c for _, c, _ in DEMO_FILES)
        assert "vector index" in all_content.lower()

    def test_semantic_search_friendly(self) -> None:
        """At least one file should discuss auth flow for semantic search."""
        all_content = " ".join(c for _, c, _ in DEMO_FILES)
        assert "authentication flow" in all_content.lower()


# ---------------------------------------------------------------------------
# Idempotency tests
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_seed_files_idempotent(self) -> None:
        """Running seed twice should not duplicate files in manifest."""
        from nexus.cli.commands.demo import _seed_files

        mock_nx = MagicMock()
        manifest: dict = {"files": []}

        # First seed
        count1 = _seed_files(mock_nx, manifest)
        assert count1 == len(DEMO_FILES)

        # Second seed — all paths already in manifest
        count2 = _seed_files(mock_nx, manifest)
        assert count2 == 0

    def test_seed_versions_idempotent(self) -> None:
        from nexus.cli.commands.demo import _seed_versions

        mock_nx = MagicMock()
        manifest: dict = {}

        _seed_versions(mock_nx, manifest)
        assert manifest["versions_seeded"] is True

        # Second call — should skip
        mock_nx.reset_mock()
        result = _seed_versions(mock_nx, manifest)
        assert result == 0
        mock_nx.sys_write.assert_not_called()
