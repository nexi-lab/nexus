"""CLI connector infrastructure — base classes, protocols, and configuration.

Provides the foundation for CLI-backed connectors (gws, gh, etc.):
- ConnectorSyncProvider protocol for delta sync integration
- CLIResult / CLIErrorMapper for structured subprocess error handling
- ConnectorConfig for declarative YAML-based connector configuration
- CLIContractSuite for behavioral contract compliance testing
"""

from nexus.backends.connectors.cli.config import (
    AuthConfig,
    CLIConnectorConfig,
    ReadConfig,
    SyncConfig,
    WriteOperationConfig,
)
from nexus.backends.connectors.cli.protocol import (
    ConnectorSyncProvider,
    FetchResult,
    MountSyncState,
    RemoteItem,
    SyncPage,
)
from nexus.backends.connectors.cli.result import (
    CLIErrorMapper,
    CLIResult,
    CLIResultStatus,
    ErrorMapping,
)

__all__ = [
    # Protocol
    "ConnectorSyncProvider",
    "FetchResult",
    "MountSyncState",
    "RemoteItem",
    "SyncPage",
    # Result
    "CLIErrorMapper",
    "CLIResult",
    "CLIResultStatus",
    "ErrorMapping",
    # Config
    "AuthConfig",
    "CLIConnectorConfig",
    "ReadConfig",
    "SyncConfig",
    "WriteOperationConfig",
]
