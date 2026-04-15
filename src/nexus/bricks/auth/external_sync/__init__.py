"""External CLI sync framework for discovering credentials managed by external tools."""

from nexus.bricks.auth.external_sync.aws_sync import AwsCliSyncAdapter
from nexus.bricks.auth.external_sync.base import (
    ExternalCliSyncAdapter,
    SyncedProfile,
    SyncResult,
)
from nexus.bricks.auth.external_sync.external_cli_backend import ExternalCliBackend
from nexus.bricks.auth.external_sync.file_adapter import FileAdapter
from nexus.bricks.auth.external_sync.registry import AdapterRegistry, CircuitBreaker
from nexus.bricks.auth.external_sync.subprocess_adapter import SubprocessAdapter

__all__ = [
    "AdapterRegistry",
    "AwsCliSyncAdapter",
    "CircuitBreaker",
    "ExternalCliBackend",
    "ExternalCliSyncAdapter",
    "FileAdapter",
    "SubprocessAdapter",
    "SyncedProfile",
    "SyncResult",
]
