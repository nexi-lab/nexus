"""Storage backends for Nexus."""

import importlib
import logging
import threading

from nexus.backends.backend import Backend, HandlerStatusResponse
from nexus.backends.base_blob_connector import BaseBlobStorageConnector
from nexus.backends.cache_mixin import CacheConnectorMixin, CacheEntry, SyncResult
from nexus.backends.cache_models import IMMUTABLE_VERSION, CachedReadResult
from nexus.backends.cache_service import CacheService
from nexus.backends.factory import BackendFactory

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
from nexus.core.object_store import ObjectStoreABC, WriteResult

# Optional backends — loaded on first access via __getattr__.
# Maps attribute name → (module_path, class_name).
_OPTIONAL_BACKENDS: dict[str, tuple[str, str]] = {
    "GCSBackend": ("nexus.backends.gcs", "GCSBackend"),
    "GoogleDriveConnectorBackend": (
        "nexus.backends.gdrive_connector",
        "GoogleDriveConnectorBackend",
    ),
    "GCSConnectorBackend": ("nexus.backends.gcs_connector", "GCSConnectorBackend"),
    "S3ConnectorBackend": ("nexus.backends.s3_connector", "S3ConnectorBackend"),
    "XConnectorBackend": ("nexus.backends.x_connector", "XConnectorBackend"),
    "HNConnectorBackend": ("nexus.backends.hn_connector", "HNConnectorBackend"),
    "SlackConnectorBackend": ("nexus.backends.slack_connector", "SlackConnectorBackend"),
    "LocalConnectorBackend": ("nexus.backends.local_connector", "LocalConnectorBackend"),
    "GmailConnectorBackend": ("nexus.backends.gmail_connector", "GmailConnectorBackend"),
    "GoogleCalendarConnectorBackend": (
        "nexus.backends.gcalendar_connector",
        "GoogleCalendarConnectorBackend",
    ),
}

_optional_backends_registered = False
_registration_lock = threading.Lock()
_logger = logging.getLogger(__name__)


def __getattr__(name: str) -> object:
    """Lazy-load optional backends on first attribute access."""
    if name in _OPTIONAL_BACKENDS:
        module_path, class_name = _OPTIONAL_BACKENDS[name]
        try:
            module = importlib.import_module(module_path)
            attr = getattr(module, class_name)
        except ImportError as e:
            raise AttributeError(f"Optional backend {name!r} is not available: {e}") from e
        # Cache in module globals so __getattr__ is not called again.
        globals()[name] = attr
        return attr
    raise AttributeError(f"module 'nexus.backends' has no attribute {name}")


def _register_optional_backends() -> None:
    """Import all optional backend modules to trigger @register_connector."""
    global _optional_backends_registered

    if _optional_backends_registered:
        return
    with _registration_lock:
        if _optional_backends_registered:
            return
        _optional_backends_registered = True

        seen_modules: set[str] = set()
        for module_path, _ in _OPTIONAL_BACKENDS.values():
            if module_path in seen_modules:
                continue
            seen_modules.add(module_path)
            try:
                importlib.import_module(module_path)
            except ImportError as e:
                _logger.debug("Optional backend module %s not available: %s", module_path, e)


__all__ = [
    # Base classes
    "Backend",
    "HandlerStatusResponse",
    "ObjectStoreABC",
    "WriteResult",
    "BaseBlobStorageConnector",
    "CacheConnectorMixin",
    "CacheEntry",
    "CacheService",
    "CachedReadResult",
    "IMMUTABLE_VERSION",
    "SyncResult",
    # Factory
    "BackendFactory",
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
