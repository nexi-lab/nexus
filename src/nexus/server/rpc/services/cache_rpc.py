"""Cache RPC Service — cache warmup and stats.

Issue #2056.
"""

import logging
from typing import Any

from nexus.contracts.rpc import rpc_expose

logger = logging.getLogger(__name__)


class CacheRPCService:
    """RPC surface for cache management operations."""

    def __init__(self, nexus_fs: Any) -> None:
        self._nexus_fs = nexus_fs

    @rpc_expose(description="Warm up cache for frequently accessed files")
    async def cache_warmup(
        self,
        path: str | None = None,
        user: str | None = None,
        hours: int = 24,
        depth: int = 2,
        include_content: bool = False,
        max_files: int = 100,
    ) -> dict[str, Any]:
        from nexus.bricks.cache.warmer import CacheWarmer, WarmupConfig

        config = WarmupConfig(
            path=path,
            user=user,
            hours=hours,
            depth=depth,
            include_content=include_content,
            max_files=max_files,
        )
        warmer = CacheWarmer(self._nexus_fs)
        result = await warmer.warmup(config)
        return {
            "files_warmed": result.files_warmed,
            "duration_ms": result.duration_ms,
        }

    @rpc_expose(description="Get cache statistics")
    async def cache_stats(self) -> dict[str, Any]:
        content_cache = getattr(self._nexus_fs, "_content_cache", None)
        if content_cache is None:
            return {"enabled": False}
        stats: dict[str, Any] = content_cache.get_stats()
        return stats

    @rpc_expose(description="Get hot files by access frequency")
    async def cache_hot_files(self, limit: int = 10) -> dict[str, Any]:
        tracker = getattr(self._nexus_fs, "_file_access_tracker", None)
        if tracker is None:
            return {"files": []}
        files = tracker.get_hot_files(limit=limit)
        return {
            "files": [
                {"path": f.path, "access_count": f.access_count, "last_access": f.last_access}
                for f in files
            ],
        }
