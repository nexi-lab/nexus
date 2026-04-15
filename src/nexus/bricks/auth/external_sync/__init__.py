"""External CLI sync framework for discovering credentials managed by external tools."""

from nexus.bricks.auth.external_sync.base import (
    ExternalCliSyncAdapter,
    SyncedProfile,
    SyncResult,
)

__all__ = [
    "ExternalCliSyncAdapter",
    "SyncedProfile",
    "SyncResult",
]
