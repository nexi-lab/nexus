"""Storage backends for Nexus."""

import importlib
import logging
import threading

from nexus.backends.base.backend import Backend, HandlerStatusResponse
from nexus.backends.base.cas_addressing_engine import CASAddressingEngine, CASBackend
from nexus.backends.base.factory import BackendFactory
from nexus.backends.base.path_addressing_engine import PathAddressingEngine, PathBackend
from nexus.backends.base.registry import (
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
# Backends whose dependencies may be unavailable in slim images (e.g. remote-only)
# are loaded lazily to avoid ImportError on startup.
_OPTIONAL_BACKENDS: dict[str, tuple[str, str]] = {
    # Cache layer (depends on sqlalchemy)
    "CachedReadResult": ("nexus.backends.cache.models", "CachedReadResult"),
    "IMMUTABLE_VERSION": ("nexus.backends.cache.models", "IMMUTABLE_VERSION"),
    "CacheService": ("nexus.backends.cache.service", "CacheService"),
    "CacheConnectorMixin": ("nexus.backends.wrappers.cache_mixin", "CacheConnectorMixin"),
    "CacheEntry": ("nexus.backends.wrappers.cache_mixin", "CacheEntry"),
    "SyncResult": ("nexus.backends.wrappers.cache_mixin", "SyncResult"),
    # Storage backends
    "CASLocalBackend": ("nexus.backends.storage.cas_local", "CASLocalBackend"),
    "PathLocalBackend": ("nexus.backends.storage.path_local", "PathLocalBackend"),
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
    # GWS CLI connectors (Issue #3148 — gws-backed replacements)
    "GmailCLIConnector": (
        "nexus.backends.connectors.gws.connector",
        "GmailConnector",
    ),
    "CalendarCLIConnector": (
        "nexus.backends.connectors.gws.connector",
        "CalendarConnector",
    ),
    "SheetsCLIConnector": (
        "nexus.backends.connectors.gws.connector",
        "SheetsConnector",
    ),
    "DocsCLIConnector": (
        "nexus.backends.connectors.gws.connector",
        "DocsConnector",
    ),
    "ChatCLIConnector": (
        "nexus.backends.connectors.gws.connector",
        "ChatConnector",
    ),
    "DriveCLIConnector": (
        "nexus.backends.connectors.gws.connector",
        "DriveConnector",
    ),
    "GitHubCLIConnector": (
        "nexus.backends.connectors.github.connector",
        "GitHubConnector",
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
    """Import all optional backend modules to trigger @register_connector.

    Also scans Python entry points in the ``nexus.connectors`` group
    for externally installed connector plugins.
    """
    global _optional_backends_registered

    if _optional_backends_registered:
        return
    with _registration_lock:
        if _optional_backends_registered:
            return
        _optional_backends_registered = True

        # --- Built-in optional backends ---
        seen_modules: set[str] = set()
        for module_path, _ in _OPTIONAL_BACKENDS.values():
            if module_path in seen_modules:
                continue
            seen_modules.add(module_path)
            try:
                importlib.import_module(module_path)
            except ImportError as e:
                _logger.debug("Optional backend module %s not available: %s", module_path, e)

        # --- External connector plugins via entry points (Issue #3148, Decision #4) ---
        try:
            from importlib.metadata import entry_points

            for ep in entry_points(group="nexus.connectors"):
                try:
                    ep.load()
                except (ImportError, ModuleNotFoundError):
                    _logger.debug("Connector plugin %s not installed, skipping", ep.name)
                except Exception:
                    _logger.warning("Connector plugin %s failed to load", ep.name, exc_info=True)
        except Exception:
            _logger.debug("Entry point scanning unavailable")

        # --- CLI connector configs from config directory (Issue #3148, Phase 5) ---
        # Scan ~/.nexus/connectors/ or NEXUS_CONNECTORS_DIR for YAML configs
        import os
        from pathlib import Path

        config_dir_env = os.getenv("NEXUS_CONNECTORS_DIR")
        config_dirs = []
        if config_dir_env:
            config_dirs.append(Path(config_dir_env))
        config_dirs.append(Path.home() / ".nexus" / "connectors")

        for config_dir in config_dirs:
            if not config_dir.is_dir():
                continue
            try:
                from nexus.backends.connectors.cli.loader import (
                    create_connector_class_from_yaml,
                    load_all_configs,
                )

                configs = load_all_configs(config_dir)
                for name, config in configs.items():
                    try:
                        from nexus.backends.base.registry import ConnectorRegistry

                        # Create a dedicated subclass with baked-in config
                        # so ConnectorRegistry gets a proper class, not a
                        # generic CLIConnector that lost its config.
                        connector_cls = create_connector_class_from_yaml(name, config)
                        ConnectorRegistry.register(
                            name=f"cli:{name}",
                            connector_class=connector_cls,
                            description=f"CLI connector: {config.cli} {config.service}",
                            category="cli",
                        )
                        _logger.info(
                            "Registered CLI connector from config: %s (%s %s)",
                            name,
                            config.cli,
                            config.service,
                        )
                    except Exception:
                        _logger.warning(
                            "Failed to register CLI connector %s from %s",
                            name,
                            config_dir,
                            exc_info=True,
                        )
            except (ImportError, ModuleNotFoundError):
                _logger.debug("CLI connector loader not available")
                break  # If loader isn't available, skip all dirs


__all__ = [
    # Base classes
    "Backend",
    "HandlerStatusResponse",
    "ObjectStoreABC",
    "WriteResult",
    "CASAddressingEngine",
    "CASBackend",
    "PathAddressingEngine",
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
    "PathLocalBackend",
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
