"""External CLI sync framework for discovering credentials managed by external tools."""

from nexus.bricks.auth.external_sync.aws_sync import AwsCliSyncAdapter
from nexus.bricks.auth.external_sync.base import (
    ExternalCliSyncAdapter,
    SyncedProfile,
    SyncResult,
)
from nexus.bricks.auth.external_sync.file_adapter import FileAdapter
from nexus.bricks.auth.external_sync.subprocess_adapter import SubprocessAdapter

__all__ = [
    "AwsCliSyncAdapter",
    "ExternalCliSyncAdapter",
    "FileAdapter",
    "SubprocessAdapter",
    "SyncedProfile",
    "SyncResult",
]
