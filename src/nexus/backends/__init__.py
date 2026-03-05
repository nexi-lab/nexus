"""Storage backends for Nexus."""

import importlib
import logging
import threading

from nexus.backends.base.backend import Backend, HandlerStatusResponse
from nexus.backends.base.cas_backend import CASBackend
from nexus.backends.base.factory import BackendFactory
from nexus.backends.base.path_backend import PathBackend
from nexus.backends.base.registry import (
    ArgType,
    ConnectionArg,
    ConnectorInfo,
    ConnectorRegistry,
    create_connector,
    create_connector_from_config,
    register_connector,
)
from nexus.backends.cache.models import IMMUTABLE_VERSION, CachedReadResult
from nexus.backends.cache.service import CacheService

# Core backends (always available)
from nexus.backends.storage.cas_local import CASLocalBackend
from nexus.backends.storage.passthrough import PassthroughBackend
from nexus.backends.wrappers.cache_mixin import CacheConnectorMixin, CacheEntry, SyncResult
from nexus.core.object_store import ObjectStoreABC, WriteResult

# Optional backends — loaded on first access via __getattr__.
# Maps attribute name → (module_path, class_name).
_OPTIONAL_BACKENDS: dict[str, tuple[str, str]] = {
    "CASGCSBackend": ("nexus.backends.storage.cas_gcs", "CASGCSBackend"),
    "GoogleDriveConnectorBackend": (
        "nexus.backends.connectors.gdrive.connector",
        "GoogleDriveConnectorBackend",
    ),
    "PathGCSBackend": ("nexus.backends.storage.path_gcs", "PathGCSBackend"),
    "PathS3Backend": ("nexus.backends.storage.path_s3", "PathS3Backend"),
    "XConnectorBackend": ("nexus.backends.connectors.x.connector", "XConnectorBackend"),
    "HNConnectorBackend": ("nexus.backends.connectors.hn.connector", "HNConnectorBackend"),
    "SlackConnectorBackend": ("nexus.backends.connectors.slack.connector", "SlackConnectorBackend"),
    "LocalConnectorBackend": ("nexus.backends.storage.local_connector", "LocalConnectorBackend"),
    "GmailConnectorBackend": ("nexus.backends.connectors.gmail.connector", "GmailConnectorBackend"),
    "GoogleCalendarConnectorBackend": (
        "nexus.backends.connectors.calendar.connector",
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
    "CASBackend",
    "PathBackend",
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
    "CASLocalBackend",
    "PassthroughBackend",
    "CASGCSBackend",
    "GoogleDriveConnectorBackend",
    "PathGCSBackend",
    "PathS3Backend",
    "XConnectorBackend",
    "HNConnectorBackend",
    "SlackConnectorBackend",
    "LocalConnectorBackend",
    "GmailConnectorBackend",
    "GoogleCalendarConnectorBackend",
]
