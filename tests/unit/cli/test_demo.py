"""Tests for nexus.cli.commands.demo — demo init/reset."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from nexus.cli.commands.demo import (
    DEMO_AGENTS,
    DEMO_DIRS,
    DEMO_FILES,
    DEMO_USERS,
    MANIFEST_FILENAME,
    PLAN_VERSIONS,
    _delete_demo_files,
    _load_manifest,
    _save_manifest,
    _seed_identities,
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

    def test_demo_dirs_ordered_parents_first(self) -> None:
        """Directories should be ordered so parents come before children."""
        for i in range(1, len(DEMO_DIRS)):
            parent = "/".join(DEMO_DIRS[i].split("/")[:-1])
            # Parent should appear before child in the list
            parent_indices = [j for j, d in enumerate(DEMO_DIRS) if d == parent]
            if parent_indices:
                assert parent_indices[0] < i


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

    def test_seed_permissions_idempotent(self) -> None:
        from nexus.cli.commands.demo import _seed_permissions

        mock_nx = MagicMock()
        # No rebac_manager available
        mock_nx._rebac_manager = None
        mock_nx.rebac_manager = None
        manifest: dict = {}

        _seed_permissions(mock_nx, manifest)
        assert manifest["permissions_seeded"] is True

        # Second call — should skip
        result = _seed_permissions(mock_nx, manifest)
        assert result == 0

    def test_seed_permissions_with_rebac(self) -> None:
        from nexus.cli.commands.demo import _seed_permissions

        mock_rebac = MagicMock()
        mock_nx = MagicMock()
        mock_nx._rebac_manager = mock_rebac
        manifest: dict = {}

        created = _seed_permissions(mock_nx, manifest)
        assert created == 3
        assert mock_rebac.rebac_write.call_count == 3


# ---------------------------------------------------------------------------
# Delete / reset tests
# ---------------------------------------------------------------------------


class TestDeleteDemoFiles:
    def test_deletes_files_via_sys_unlink(self) -> None:
        """Should call sys_unlink (not sys_rm) for each file."""
        mock_nx = MagicMock()
        manifest = {"files": ["/workspace/demo/a.txt", "/workspace/demo/b.py"]}

        removed = _delete_demo_files(mock_nx, manifest)
        assert removed == 2

        # Verify sys_unlink was called (not sys_rm)
        assert mock_nx.sys_unlink.call_count == 2
        # Should also try to remove directories
        assert mock_nx.sys_rmdir.call_count == len(DEMO_DIRS)

    def test_delete_empty_manifest(self) -> None:
        mock_nx = MagicMock()
        removed = _delete_demo_files(mock_nx, {"files": []})
        assert removed == 0

    def test_delete_tolerates_errors(self) -> None:
        """Errors during deletion should not propagate."""
        mock_nx = MagicMock()
        mock_nx.sys_unlink.side_effect = Exception("not found")
        mock_nx.sys_rmdir.side_effect = Exception("not empty")
        manifest = {"files": ["/workspace/demo/a.txt"]}

        removed = _delete_demo_files(mock_nx, manifest)
        assert removed == 0  # All failed, but no exception raised


# ---------------------------------------------------------------------------
# Identity seeding tests
# ---------------------------------------------------------------------------


class TestSeedIdentities:
    def test_skips_non_database_auth(self) -> None:
        config = {"preset": "shared", "auth": "static"}
        manifest: dict = {}
        created = _seed_identities(config, manifest)
        assert created == 0
        assert manifest["identities_seeded"] is True

    def test_idempotent(self) -> None:
        config = {"preset": "demo", "auth": "database", "api_key": "test-key"}
        manifest: dict = {"identities_seeded": True}
        created = _seed_identities(config, manifest)
        assert created == 0

    def test_provisions_via_rpc(self, tmp_path: Path) -> None:
        """When admin RPC is available, should create demo_user + demo_agent."""
        key_file = tmp_path / ".admin-api-key"
        key_file.write_text("test-admin-key")

        mock_rpc = MagicMock(return_value={"api_key": "new-key", "user_id": "x"})
        mock_get_rpc = MagicMock(return_value=mock_rpc)

        config = {
            "preset": "demo",
            "auth": "database",
            "data_dir": str(tmp_path),
            "ports": {"http": 2026, "grpc": 2028},
        }
        manifest: dict = {}

        # Patch the admin module that _seed_identities imports from
        mock_admin_module = MagicMock(get_admin_rpc=mock_get_rpc)
        with patch.dict("sys.modules", {"nexus.cli.commands.admin": mock_admin_module}):
            created = _seed_identities(config, manifest)

        # Should create demo_user (skip admin) + demo_agent = 2
        assert created == 2
        assert manifest["identities_seeded"] is True
        assert "identity_keys" in manifest

    def test_skips_when_no_api_key(self) -> None:
        config = {"preset": "demo", "auth": "database", "data_dir": "/nonexistent"}
        manifest: dict = {}
        created = _seed_identities(config, manifest)
        assert created == 0
