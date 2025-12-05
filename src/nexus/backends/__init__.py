"""Storage backends for Nexus."""

from nexus.backends.backend import Backend
from nexus.backends.base_blob_connector import BaseBlobStorageConnector
from nexus.backends.cache_mixin import CacheConnectorMixin, CacheEntry, SyncResult

# Core backends (always available)
from nexus.backends.local import LocalBackend
from nexus.backends.registry import (
    ArgType,
    ConnectionArg,
    ConnectorInfo,
    ConnectorRegistry,
    create_connector,
    create_connector_from_config,
    register_connector,
)

# Optional backends (require extra dependencies)
# Import triggers @register_connector decorator, registering each backend
try:
    from nexus.backends.gcs import GCSBackend
except ImportError:
    GCSBackend = None  # type: ignore

try:
    from nexus.backends.gdrive_connector import GoogleDriveConnectorBackend
except ImportError:
    GoogleDriveConnectorBackend = None  # type: ignore

try:
    from nexus.backends.gcs_connector import GCSConnectorBackend
except ImportError:
    GCSConnectorBackend = None  # type: ignore

try:
    from nexus.backends.s3_connector import S3ConnectorBackend
except ImportError:
    S3ConnectorBackend = None  # type: ignore

try:
    from nexus.backends.x_connector import XConnectorBackend
except ImportError:
    XConnectorBackend = None  # type: ignore

try:
    from nexus.backends.hn_connector import HNConnectorBackend
except ImportError:
    HNConnectorBackend = None  # type: ignore

__all__ = [
    # Base classes
    "Backend",
    "BaseBlobStorageConnector",
    "CacheConnectorMixin",
    "CacheEntry",
    "SyncResult",
    # Registry
    "ConnectorRegistry",
    "ConnectorInfo",
    "ConnectionArg",
    "ArgType",
    "register_connector",
    "create_connector",
    "create_connector_from_config",
    # Concrete backends
    "LocalBackend",
    "GCSBackend",
    "GoogleDriveConnectorBackend",
    "GCSConnectorBackend",
    "S3ConnectorBackend",
    "XConnectorBackend",
    "HNConnectorBackend",
]
