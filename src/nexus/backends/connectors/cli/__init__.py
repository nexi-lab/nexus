"""CLI connector infrastructure — base classes, protocols, and configuration.

Provides the foundation for CLI-backed connectors (gws, gh, etc.):
- CLIConnector base class implementing ContentStoreProtocol
- ConnectorSyncProvider protocol for delta sync integration
- CLISyncProvider bridging sync protocol to CLI list/fetch
- CLIResult / CLIErrorMapper for structured subprocess error handling
- CLIConnectorConfig for declarative YAML-based connector configuration
- CLIContractSuite for behavioral contract compliance testing
- YAML config loader for declarative connector instantiation
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
    # Base class
    "CLIConnector",
    # Sync
    "CLISyncProvider",
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
    # Loader
    "create_connector_from_yaml",
    "load_connector_config",
]


def __getattr__(name: str) -> object:
    """Lazy-load heavy modules to keep import time low."""
    if name == "CLIConnector":
        from nexus.backends.connectors.cli.base import CLIConnector

        return CLIConnector
    if name == "CLISyncProvider":
        from nexus.backends.connectors.cli.sync_provider import CLISyncProvider

        return CLISyncProvider
    if name in ("load_connector_config", "create_connector_from_yaml"):
        from nexus.backends.connectors.cli import loader

        return getattr(loader, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
