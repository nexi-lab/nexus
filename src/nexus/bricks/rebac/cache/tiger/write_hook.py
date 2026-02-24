"""TigerCacheWriteHook — add new files to ancestor directory grants (INTERCEPT write).

When a file is created in a directory that has been granted to users,
the file should inherit those permissions (if include_future_files=True).

Same architectural pattern as TigerCacheRenameHook:
    contracts define VFSWriteHook Protocol →
    bricks implements TigerCacheWriteHook →
    factory registers at boot via KernelDispatch →
    kernel dispatches without knowing tiger cache exists.

Issue #2133: Extracted from NexusFSCoreMixin inline code (Leopard-style grants).
"""

import logging
from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.vfs_hooks import WriteHookContext

logger = logging.getLogger(__name__)


class TigerCacheWriteHook:
    """Post-write hook that adds newly created files to ancestor directory grants.

    Dependencies injected at construction:
      - tiger_cache: The TigerCache instance (bitmap_cache)
    """

    def __init__(self, tiger_cache: Any) -> None:
        self._tiger_cache = tiger_cache

    @property
    def name(self) -> str:
        return "tiger_cache_write"

    def on_post_write(self, ctx: WriteHookContext) -> None:
        if not ctx.is_new_file:
            return
        if self._tiger_cache is None:
            return

        zone_id = ctx.zone_id or ROOT_ZONE_ID
        added_count = self._tiger_cache.add_file_to_ancestor_grants(
            file_path=ctx.path,
            zone_id=zone_id,
        )
        if added_count > 0:
            logger.debug(
                "[LEOPARD] New file %s added to %d ancestor directory grants",
                ctx.path,
                added_count,
            )
