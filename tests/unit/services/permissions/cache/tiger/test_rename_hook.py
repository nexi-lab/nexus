"""Unit tests for TigerCacheRenameHook."""

from __future__ import annotations

from unittest.mock import MagicMock

from nexus.contracts.vfs_hooks import RenameHookContext
from nexus.services.permissions.cache.tiger.rename_hook import TigerCacheRenameHook


class TestTigerCacheRenameHook:
    def _make_tiger_cache(
        self,
        old_grants: list[dict] | None = None,
        new_grants: list[dict] | None = None,
    ) -> MagicMock:
        tc = MagicMock()
        tc.get_directory_grants_for_path = MagicMock(
            side_effect=lambda path, zone: old_grants or [] if "old" in path else new_grants or []
        )
        tc._resource_map = MagicMock()
        tc._resource_map.get_or_create_int_id = MagicMock(return_value=42)
        return tc

    def test_skips_if_no_tiger_cache(self):
        hook = TigerCacheRenameHook(tiger_cache=None)
        ctx = RenameHookContext(old_path="/old/file.txt", new_path="/new/file.txt", context=None)
        hook.on_post_rename(ctx)  # should not raise

    def test_skips_if_no_grant_changes(self):
        tc = self._make_tiger_cache(
            old_grants=[{"subject_type": "user", "subject_id": "alice", "permission": "read"}],
            new_grants=[{"subject_type": "user", "subject_id": "alice", "permission": "read"}],
        )
        hook = TigerCacheRenameHook(tiger_cache=tc)
        ctx = RenameHookContext(
            old_path="/old/file.txt", new_path="/new/file.txt", context=None, zone_id="z1"
        )
        hook.on_post_rename(ctx)

        # No bitmap operations since grants are the same
        tc.remove_from_bitmap.assert_not_called()
        tc.add_to_bitmap.assert_not_called()

    def test_removes_from_old_grants(self):
        tc = self._make_tiger_cache(
            old_grants=[{"subject_type": "user", "subject_id": "alice", "permission": "read"}],
            new_grants=[],
        )
        hook = TigerCacheRenameHook(tiger_cache=tc)
        ctx = RenameHookContext(
            old_path="/old/file.txt",
            new_path="/new/file.txt",
            context=None,
            zone_id="z1",
        )
        hook.on_post_rename(ctx)

        tc.remove_from_bitmap.assert_called_once()
        tc.add_to_bitmap.assert_not_called()

    def test_adds_to_new_grants(self):
        tc = self._make_tiger_cache(
            old_grants=[],
            new_grants=[
                {
                    "subject_type": "user",
                    "subject_id": "bob",
                    "permission": "write",
                    "include_future_files": True,
                }
            ],
        )
        hook = TigerCacheRenameHook(tiger_cache=tc)
        ctx = RenameHookContext(
            old_path="/old/file.txt",
            new_path="/new/file.txt",
            context=None,
            zone_id="z1",
        )
        hook.on_post_rename(ctx)

        tc.add_to_bitmap.assert_called_once()
        tc.persist_single_grant.assert_called_once()

    def test_directory_rename_lists_children(self):
        tc = self._make_tiger_cache(
            old_grants=[{"subject_type": "user", "subject_id": "alice", "permission": "read"}],
            new_grants=[],
        )

        # Mock metadata listing with 2 child files
        child1 = MagicMock()
        child1.path = "/new/dir/a.txt"
        child2 = MagicMock()
        child2.path = "/new/dir/b.txt"

        hook = TigerCacheRenameHook(
            tiger_cache=tc,
            metadata_list_iter=lambda **kwargs: [child1, child2],
        )
        ctx = RenameHookContext(
            old_path="/old/dir",
            new_path="/new/dir",
            context=None,
            zone_id="z1",
            is_directory=True,
        )
        hook.on_post_rename(ctx)

        # remove_from_bitmap called for each child file
        assert tc.remove_from_bitmap.call_count == 2

    def test_name(self):
        hook = TigerCacheRenameHook(tiger_cache=None)
        assert hook.name == "tiger_cache_rename"

    def test_no_resource_map_skips(self):
        tc = MagicMock()
        tc.get_directory_grants_for_path = MagicMock(
            side_effect=lambda path, zone: (
                [{"subject_type": "user", "subject_id": "x", "permission": "r"}]
                if "old" in path
                else []
            )
        )
        tc._resource_map = None  # no resource map

        hook = TigerCacheRenameHook(tiger_cache=tc)
        ctx = RenameHookContext(
            old_path="/old/f.txt", new_path="/new/f.txt", context=None, zone_id="z1"
        )
        hook.on_post_rename(ctx)  # should not raise
