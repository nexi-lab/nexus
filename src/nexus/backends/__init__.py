"""Storage backends for Nexus."""

from nexus.backends.backend import Backend, HandlerStatusResponse
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

# Optional backends - LAZY IMPORTS for faster CLI startup
# These are imported on-demand when actually used, not at module load time
# This saves ~500ms+ of startup time by avoiding google.cloud imports
GCSBackend = None
GoogleDriveConnectorBackend = None
GCSConnectorBackend = None
S3ConnectorBackend = None
XConnectorBackend = None
HNConnectorBackend = None


def _register_optional_backends() -> None:
    """Register optional backends on first use (lazy loading)."""
    global GCSBackend, GoogleDriveConnectorBackend, GCSConnectorBackend
    global S3ConnectorBackend, XConnectorBackend, HNConnectorBackend

    # Only register once
    if GCSBackend is not None:
        return

    try:
        from nexus.backends.gcs import GCSBackend as _GCSBackend

        GCSBackend = _GCSBackend
    except ImportError:
        pass

    try:
        from nexus.backends.gdrive_connector import GoogleDriveConnectorBackend as _GDrive

        GoogleDriveConnectorBackend = _GDrive
    except ImportError:
        pass

    try:
        from nexus.backends.gcs_connector import GCSConnectorBackend as _GCSConn

        GCSConnectorBackend = _GCSConn
    except ImportError:
        pass

    try:
        from nexus.backends.s3_connector import S3ConnectorBackend as _S3Conn

        S3ConnectorBackend = _S3Conn
    except ImportError:
        pass

    try:
        from nexus.backends.x_connector import XConnectorBackend as _XConn

        XConnectorBackend = _XConn
    except ImportError:
        pass

    try:
        from nexus.backends.hn_connector import HNConnectorBackend as _HNConn

        HNConnectorBackend = _HNConn
    except ImportError:
        pass


__all__ = [
    # Base classes
    "Backend",
    "HandlerStatusResponse",
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
