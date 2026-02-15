"""Storage backends for Nexus."""

from nexus.backends.backend import Backend, HandlerStatusResponse
from nexus.backends.base_blob_connector import BaseBlobStorageConnector
from nexus.backends.cache_mixin import CacheConnectorMixin, CacheEntry, SyncResult

# Core backends (always available)
from nexus.backends.local import LocalBackend
from nexus.backends.passthrough import PassthroughBackend
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
SlackConnectorBackend = None
LocalConnectorBackend = None
GmailConnectorBackend = None
GoogleCalendarConnectorBackend = None


_optional_backends_registered = False


def _register_optional_backends() -> None:
    """Register optional backends on first use (lazy loading)."""
    global _optional_backends_registered
    global GCSBackend, GoogleDriveConnectorBackend, GCSConnectorBackend
    global S3ConnectorBackend, XConnectorBackend, HNConnectorBackend, SlackConnectorBackend
    global LocalConnectorBackend, GmailConnectorBackend, GoogleCalendarConnectorBackend

    # Only register once
    if _optional_backends_registered:
        return
    _optional_backends_registered = True

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

    try:
        from nexus.backends.slack_connector import SlackConnectorBackend as _SlackConn

        SlackConnectorBackend = _SlackConn
    except ImportError:
        pass

    # LocalConnectorBackend - no external deps, but kept here for consistency with other connectors
    try:
        from nexus.backends.local_connector import LocalConnectorBackend as _LocalConn

        LocalConnectorBackend = _LocalConn
    except ImportError:
        pass

    try:
        from nexus.backends.gmail_connector import GmailConnectorBackend as _GmailConn

        GmailConnectorBackend = _GmailConn
    except ImportError:
        pass

    try:
        from nexus.backends.gcalendar_connector import (
            GoogleCalendarConnectorBackend as _GCalConn,
        )

        GoogleCalendarConnectorBackend = _GCalConn
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
    "PassthroughBackend",
    "GCSBackend",
    "GoogleDriveConnectorBackend",
    "GCSConnectorBackend",
    "S3ConnectorBackend",
    "XConnectorBackend",
    "HNConnectorBackend",
    "SlackConnectorBackend",
    "LocalConnectorBackend",
    "GmailConnectorBackend",
    "GoogleCalendarConnectorBackend",
]
