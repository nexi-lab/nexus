"""External CLI sync framework for discovering credentials managed by external tools."""

from nexus.bricks.auth.external_sync.base import (
    ExternalCliSyncAdapter,
    SyncedProfile,
    SyncResult,
)
from nexus.bricks.auth.external_sync.subprocess_adapter import SubprocessAdapter

__all__ = [
    "ExternalCliSyncAdapter",
    "SubprocessAdapter",
    "SyncedProfile",
    "SyncResult",
]
