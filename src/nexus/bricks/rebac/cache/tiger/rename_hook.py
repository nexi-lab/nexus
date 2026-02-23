"""TigerCacheRenameHook — bitmap updates on file/directory move (INTERCEPT rename).

Extracted from NexusFSCoreMixin._update_tiger_cache_on_move() +
_get_directory_files_for_move() (Phase 4 of Issue #2033, Strangler Fig decomposition).
Issue #625: Lives in services/permissions/cache/tiger/ (service-layer, not kernel).
"""

import logging
from collections.abc import Callable
from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.vfs_hooks import RenameHookContext

logger = logging.getLogger(__name__)


class TigerCacheRenameHook:
    """Post-rename hook that updates tiger cache bitmaps on file/directory moves.

    Dependencies injected at construction:
      - tiger_cache:   The TigerCache instance (may be None)
      - metadata_list: (prefix, recursive, zone_id) -> Iterator[FileMetadata]
    """

    def __init__(
        self,
        tiger_cache: Any | None,
        metadata_list_iter: Callable[..., Any] | None = None,
    ) -> None:
        self._tiger_cache = tiger_cache
        self._metadata_list_iter = metadata_list_iter

    @property
    def name(self) -> str:
        return "tiger_cache_rename"

    def on_post_rename(self, ctx: RenameHookContext) -> None:
        if self._tiger_cache is None:
            return

        zone_id = ctx.zone_id or ROOT_ZONE_ID
        old_grants = self._tiger_cache.get_directory_grants_for_path(ctx.old_path, zone_id)
        new_grants = self._tiger_cache.get_directory_grants_for_path(ctx.new_path, zone_id)

        def grant_key(g: dict) -> tuple:
            return (g["subject_type"], g["subject_id"], g["permission"])

        old_keys = {grant_key(g) for g in old_grants}
        new_keys = {grant_key(g) for g in new_grants}

        grants_to_remove = old_keys - new_keys
        grants_to_add = new_keys - old_keys

        if not grants_to_remove and not grants_to_add:
            return

        if ctx.is_directory:
            files = self._get_directory_files(ctx.old_path, ctx.new_path, zone_id)
        else:
            files = [(ctx.old_path, ctx.new_path)]

        resource_map = getattr(self._tiger_cache, "_resource_map", None)
        if not resource_map:
            return

        for _old_file, new_file in files:
            int_id = resource_map.get_or_create_int_id("file", new_file)
            if int_id <= 0:
                continue

            for subject_type, subject_id, permission in grants_to_remove:
                try:
                    self._tiger_cache.remove_from_bitmap(
                        subject_type=subject_type,
                        subject_id=subject_id,
                        permission=permission,
                        resource_type="file",
                        zone_id=zone_id,
                        resource_int_id=int_id,
                    )
                except Exception as e:
                    logger.warning(f"[LEOPARD] Failed to remove from bitmap: {e}")

            for grant in new_grants:
                key = grant_key(grant)
                if key not in grants_to_add:
                    continue
                if not grant.get("include_future_files", True):
                    continue
                try:
                    self._tiger_cache.add_to_bitmap(
                        grant["subject_type"],
                        grant["subject_id"],
                        grant["permission"],
                        "file",
                        zone_id,
                        int_id,
                    )
                    self._tiger_cache.persist_single_grant(
                        grant["subject_type"],
                        grant["subject_id"],
                        grant["permission"],
                        "file",
                        new_file,
                        zone_id,
                    )
                except Exception as e:
                    logger.warning(f"[LEOPARD] Failed to add to bitmap: {e}")

    def _get_directory_files(
        self, old_dir: str, new_dir: str, zone_id: str
    ) -> list[tuple[str, str]]:
        if self._metadata_list_iter is None:
            return []

        old_prefix = old_dir.rstrip("/") + "/"
        new_prefix = new_dir.rstrip("/") + "/"

        try:
            result = []
            for file_meta in self._metadata_list_iter(
                prefix=new_prefix, recursive=True, zone_id=zone_id
            ):
                new_file_path = file_meta.path
                if new_file_path:
                    relative_path = new_file_path[len(new_prefix) :]
                    old_file_path = old_prefix + relative_path
                    result.append((old_file_path, new_file_path))
            return result
        except Exception as e:
            logger.warning(f"[LEOPARD] Failed to list directory files: {e}")
            return []
