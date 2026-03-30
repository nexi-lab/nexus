"""Tests for nexus.cli.commands.demo — demo init/reset."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.cli.commands.demo import (
    DEMO_AGENTS,
    DEMO_DIRS,
    DEMO_FILES,
    DEMO_PERMISSION_TUPLES,
    DEMO_USERS,
    HERB_CORPUS,
    MANIFEST_FILENAME,
    PLAN_VERSIONS,
    _delete_demo_files,
    _delete_permissions,
    _load_manifest,
    _revoke_identities,
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
    @pytest.mark.asyncio
    async def test_seed_files_idempotent(self) -> None:
        """Running seed twice should not duplicate files in manifest."""
        from nexus.cli.commands.demo import _seed_files

        total_files = len(DEMO_FILES) + len(HERB_CORPUS)
        mock_nx = MagicMock()
        mock_nx.sys_write = AsyncMock()
        mock_nx.write = AsyncMock()
        mock_nx.mkdir = AsyncMock()
        mock_nx.sys_readdir = AsyncMock(return_value=[])
        mock_nx.access = AsyncMock(side_effect=[True] * total_files)
        manifest: dict = {"files": []}

        # First seed — includes both DEMO_FILES and HERB_CORPUS
        count1 = await _seed_files(mock_nx, manifest)
        assert count1 == total_files

        # Second seed — all paths already in manifest
        count2 = await _seed_files(mock_nx, manifest)
        assert count2 == 0

    @pytest.mark.asyncio
    async def test_seed_files_recreates_missing_manifest_entries(self) -> None:
        """Manifest hits should be recreated when the remote path no longer exists."""
        from nexus.cli.commands.demo import _seed_files

        total_files = len(DEMO_FILES) + len(HERB_CORPUS)
        seeded_paths = [path for path, _, _ in [*DEMO_FILES, *HERB_CORPUS]]

        mock_nx = MagicMock()
        mock_nx.sys_write = AsyncMock()
        mock_nx.write = AsyncMock()
        mock_nx.mkdir = AsyncMock()
        mock_nx.sys_readdir = AsyncMock(return_value=[])
        mock_nx.access = AsyncMock(side_effect=[False] * total_files)
        manifest: dict = {"files": list(seeded_paths)}

        recreated = await _seed_files(mock_nx, manifest)

        assert recreated == total_files
        assert manifest["files"] == seeded_paths + seeded_paths

    @pytest.mark.asyncio
    async def test_seed_versions_idempotent(self) -> None:
        from nexus.cli.commands.demo import _seed_versions

        mock_nx = MagicMock()
        mock_nx.sys_write = AsyncMock()
        manifest: dict = {}

        await _seed_versions(mock_nx, manifest)
        assert manifest["versions_seeded"] is True

        # Second call — should skip
        mock_nx.sys_write = AsyncMock()
        result = await _seed_versions(mock_nx, manifest)
        assert result == 0
        mock_nx.sys_write.assert_not_called()

    def test_seed_permissions_idempotent(self) -> None:
        from nexus.cli.commands.demo import _seed_permissions

        mock_nx = MagicMock()
        # No rebac_manager available
        mock_nx.service.return_value = None
        config: dict = {"preset": "local"}
        manifest: dict = {}

        _seed_permissions(mock_nx, config, manifest)
        assert manifest["permissions_seeded"] is True

        # Second call — should skip
        result = _seed_permissions(mock_nx, config, manifest)
        assert result == 0

    def test_seed_permissions_with_rebac(self) -> None:
        from nexus.cli.commands.demo import _seed_permissions

        mock_rebac = MagicMock()
        mock_nx = MagicMock()
        mock_nx.service.return_value = mock_rebac
        config: dict = {"preset": "local"}
        manifest: dict = {}

        created = _seed_permissions(mock_nx, config, manifest)
        assert created == 4
        assert mock_rebac.rebac_write.call_count == 4

    @patch("nexus.cli.commands.demo._seed_permissions_docker", return_value=3)
    def test_seed_permissions_shared_preset_uses_docker(self, mock_docker: MagicMock) -> None:
        """Shared/demo presets should prefer docker exec over direct rebac."""
        from nexus.cli.commands.demo import _seed_permissions

        mock_nx = MagicMock()
        config: dict = {"preset": "shared"}
        manifest: dict = {}

        created = _seed_permissions(mock_nx, config, manifest)
        assert created == 3
        mock_docker.assert_called_once()
        assert manifest["permissions_count"] == 3

    @patch("nexus.cli.commands.demo._seed_permissions_rpc", return_value=3)
    @patch("nexus.cli.commands.demo._seed_permissions_docker", return_value=-1)
    def test_seed_permissions_shared_falls_back_to_rpc(
        self, mock_docker: MagicMock, mock_rpc: MagicMock
    ) -> None:
        """When docker exec is unavailable, fall back to admin RPC."""
        from nexus.cli.commands.demo import _seed_permissions

        mock_nx = MagicMock()
        config: dict = {"preset": "demo"}
        manifest: dict = {}

        created = _seed_permissions(mock_nx, config, manifest)
        assert created == 3
        mock_docker.assert_called_once()
        mock_rpc.assert_called_once()
        assert manifest["permissions_count"] == 3


# ---------------------------------------------------------------------------
# Delete / reset tests
# ---------------------------------------------------------------------------


class TestDeleteDemoFiles:
    @pytest.mark.asyncio
    async def test_deletes_files_via_sys_unlink(self) -> None:
        """Should call sys_unlink (not sys_rm) for each file."""
        mock_nx = MagicMock()
        mock_nx.sys_unlink = AsyncMock()
        mock_nx.rmdir = AsyncMock()
        manifest = {"files": ["/workspace/demo/a.txt", "/workspace/demo/b.py"]}

        removed = await _delete_demo_files(mock_nx, manifest)
        assert removed == 2

        # Verify sys_unlink was called (not sys_rm)
        assert mock_nx.sys_unlink.call_count == 2
        # Should also try to remove directories
        assert mock_nx.rmdir.call_count == len(DEMO_DIRS)

    @pytest.mark.asyncio
    async def test_delete_empty_manifest(self) -> None:
        mock_nx = MagicMock()
        mock_nx.sys_unlink = AsyncMock()
        mock_nx.rmdir = AsyncMock()
        removed = await _delete_demo_files(mock_nx, {"files": []})
        assert removed == 0

    @pytest.mark.asyncio
    async def test_delete_tolerates_errors(self) -> None:
        """Errors during deletion should not propagate."""
        mock_nx = MagicMock()
        mock_nx.sys_unlink = AsyncMock(side_effect=Exception("not found"))
        mock_nx.rmdir = AsyncMock(side_effect=Exception("not empty"))
        manifest = {"files": ["/workspace/demo/a.txt"]}

        removed = await _delete_demo_files(mock_nx, manifest)
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

        mock_rpc = MagicMock(return_value={"api_key": "new-key", "key_id": "kid-1", "user_id": "x"})
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

        # Should create demo_user (skip admin) + demo_agent + coordinator = 3
        assert created == 3
        assert manifest["identities_seeded"] is True
        assert "identity_keys" in manifest
        # identity_keys stores dicts with api_key and key_id
        for _id, key_info in manifest["identity_keys"].items():
            assert isinstance(key_info, dict)
            assert "api_key" in key_info
            assert "key_id" in key_info

    def test_skips_when_no_api_key(self) -> None:
        config = {"preset": "demo", "auth": "database", "data_dir": "/nonexistent"}
        manifest: dict = {}
        created = _seed_identities(config, manifest)
        assert created == 0


# ---------------------------------------------------------------------------
# Revoke identity tests
# ---------------------------------------------------------------------------


class TestRevokeIdentities:
    def test_noop_when_no_keys(self) -> None:
        config = {"preset": "demo", "auth": "database"}
        manifest: dict = {}
        assert _revoke_identities(config, manifest) == 0

    def test_noop_when_no_api_key(self) -> None:
        config = {"preset": "demo", "auth": "database", "data_dir": "/nonexistent"}
        manifest = {
            "identity_keys": {
                "demo_user": {"api_key": "k1", "key_id": "kid1"},
            },
        }
        assert _revoke_identities(config, manifest) == 0

    def test_revokes_via_rpc(self, tmp_path: Path) -> None:
        key_file = tmp_path / ".admin-api-key"
        key_file.write_text("test-admin-key")

        mock_rpc = MagicMock(return_value={})
        mock_get_rpc = MagicMock(return_value=mock_rpc)

        config = {
            "preset": "demo",
            "auth": "database",
            "data_dir": str(tmp_path),
            "ports": {"http": 2026, "grpc": 2028},
        }
        manifest = {
            "identity_keys": {
                "demo_user": {"api_key": "k1", "key_id": "kid1"},
                "demo_agent": {"api_key": "k2", "key_id": "kid2"},
            },
        }

        mock_admin_module = MagicMock(get_admin_rpc=mock_get_rpc)
        with patch.dict("sys.modules", {"nexus.cli.commands.admin": mock_admin_module}):
            revoked = _revoke_identities(config, manifest)

        assert revoked == 2
        assert mock_rpc.call_count == 2

    def test_skips_entries_without_key_id(self, tmp_path: Path) -> None:
        key_file = tmp_path / ".admin-api-key"
        key_file.write_text("test-admin-key")

        mock_rpc = MagicMock(return_value={})
        mock_get_rpc = MagicMock(return_value=mock_rpc)

        config = {
            "preset": "demo",
            "auth": "database",
            "data_dir": str(tmp_path),
            "ports": {"http": 2026, "grpc": 2028},
        }
        manifest = {
            "identity_keys": {
                "demo_user": {"api_key": "k1", "key_id": ""},
                "demo_agent": {"api_key": "k2", "key_id": "kid2"},
            },
        }

        mock_admin_module = MagicMock(get_admin_rpc=mock_get_rpc)
        with patch.dict("sys.modules", {"nexus.cli.commands.admin": mock_admin_module}):
            revoked = _revoke_identities(config, manifest)

        assert revoked == 1


# ---------------------------------------------------------------------------
# Permission deletion tests
# ---------------------------------------------------------------------------


class TestDeletePermissions:
    def test_permission_tuples_constant(self) -> None:
        """DEMO_PERMISSION_TUPLES should have 3 entries matching seed logic."""
        assert len(DEMO_PERMISSION_TUPLES) == 3
        subjects = [(t["subject"][0], t["subject"][1]) for t in DEMO_PERMISSION_TUPLES]
        assert ("user", "admin") in subjects
        assert ("user", "demo_user") in subjects
        assert ("agent", "demo_agent") in subjects

    def test_delete_permissions_local_with_rebac(self) -> None:
        """Local preset should call delete_tuple for each permission tuple."""
        mock_rebac = MagicMock()
        mock_rebac.delete_tuple.return_value = True
        mock_nx = MagicMock()
        mock_nx.service.return_value = mock_rebac
        config: dict = {"preset": "local"}

        deleted = _delete_permissions(mock_nx, config)
        assert deleted == 3
        assert mock_rebac.delete_tuple.call_count == 3

    def test_delete_permissions_local_no_rebac(self) -> None:
        """When no rebac_manager is available, should return 0."""
        mock_nx = MagicMock()
        mock_nx.service.return_value = None
        config: dict = {"preset": "local"}

        deleted = _delete_permissions(mock_nx, config)
        assert deleted == 0

    @patch("nexus.cli.commands.demo._delete_permissions_docker", return_value=3)
    def test_delete_permissions_shared_uses_docker(self, mock_docker: MagicMock) -> None:
        """Shared/demo presets should use docker exec to delete permissions."""
        mock_nx = MagicMock()
        config: dict = {"preset": "shared"}

        deleted = _delete_permissions(mock_nx, config)
        assert deleted == 3
        mock_docker.assert_called_once_with(config)

    @patch("nexus.cli.commands.demo._delete_permissions_docker", return_value=-1)
    def test_delete_permissions_shared_docker_unavailable(self, mock_docker: MagicMock) -> None:
        """When docker exec is unavailable, should return 0 (best-effort)."""
        mock_nx = MagicMock()
        config: dict = {"preset": "demo"}

        deleted = _delete_permissions(mock_nx, config)
        assert deleted == 0
        mock_docker.assert_called_once()
