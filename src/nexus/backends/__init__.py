"""Storage backends for Nexus."""

from nexus.backends.backend import Backend
from nexus.backends.base_blob_connector import BaseBlobStorageConnector
from nexus.backends.local import LocalBackend

# Optional backends (require extra dependencies)
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
    from nexus.backends.gmail_connector import GmailConnectorBackend
except ImportError:
    GmailConnectorBackend = None  # type: ignore

__all__ = [
    "Backend",
    "BaseBlobStorageConnector",
    "LocalBackend",
    "GoogleDriveConnectorBackend",
    "GCSConnectorBackend",
    "S3ConnectorBackend",
    "XConnectorBackend",
    "GmailConnectorBackend",
]
