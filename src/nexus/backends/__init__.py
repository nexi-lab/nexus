"""Storage backends for Nexus.

See docs/architecture/backend-architecture.md for the Transport × Addressing
composition model and docs/architecture/connector-transport-matrix.md for
per-connector implementation details.
"""

import importlib
import logging
import os
import threading
from pathlib import Path

from nexus.backends.base.backend import Backend, HandlerStatusResponse
from nexus.backends.base.cas_addressing_engine import CASAddressingEngine
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

# Derived from CONNECTOR_MANIFEST for backward-compatible attribute access:
#   `from nexus.backends import PathGCSBackend` → lazily imports the module
# and returns the named attribute. Populated at import time from the
# single source of truth in ``nexus.backends._manifest``.
_OPTIONAL_BACKENDS: dict[str, tuple[str, str]] = {}


def _populate_optional_backends_map() -> None:
    """Populate _OPTIONAL_BACKENDS from the manifest (one-time)."""
    from nexus.backends._manifest import CONNECTOR_MANIFEST

    for entry in CONNECTOR_MANIFEST:
        _OPTIONAL_BACKENDS[entry.class_name] = (entry.module_path, entry.class_name)


_populate_optional_backends_map()

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
    """Pre-register manifest placeholders, then attempt module imports.

    Phase 1: read ``CONNECTOR_MANIFEST`` and register a placeholder
    ``ConnectorInfo`` for every entry. No imports happen.

    Phase 2: import each manifest entry's ``module_path``. On success the
    module's ``@register_connector("name")`` binds the real class into
    the placeholder (see ``ConnectorRegistry.register()``). On ImportError
    the placeholder remains — ``BackendFactory.create()`` will raise
    ``MissingDependencyError`` with the manifest's install hints.

    Phase 3: scan external entry points + YAML configs (unchanged).
    """
    global _optional_backends_registered

    if _optional_backends_registered:
        return
    with _registration_lock:
        if _optional_backends_registered:
            return
        _optional_backends_registered = True

        from nexus.backends._manifest import CONNECTOR_MANIFEST

        # Phase 1: placeholders from manifest
        for entry in CONNECTOR_MANIFEST:
            ConnectorRegistry.register_placeholder(entry)

        # Phase 2: attempt module imports; successful imports run
        # @register_connector which binds the class into the placeholder.
        seen_modules: set[str] = set()
        for entry in CONNECTOR_MANIFEST:
            if entry.module_path in seen_modules:
                continue
            seen_modules.add(entry.module_path)
            try:
                importlib.import_module(entry.module_path)
            except ImportError as e:
                _logger.debug(
                    "Connector module %s not available: %s "
                    "(placeholder stays; mount will raise MissingDependencyError)",
                    entry.module_path,
                    e,
                )

        # Phase 3: external plugins via entry points (Issue #3148, Decision #4)
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

        # Phase 3 (cont.): CLI connector configs from config directory
        # Scan ~/.nexus/connectors/ or NEXUS_CONNECTORS_DIR for YAML configs
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
                        # Create a dedicated subclass with baked-in config
                        # so ConnectorRegistry gets a proper class, not a
                        # generic PathCLIBackend that lost its config.
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
    "PathAddressingEngine",
    "PathBackend",
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
    "PathGDriveBackend",
    "PathGCSBackend",
    "PathS3Backend",
    "PathXBackend",
    "PathHNBackend",
    "PathSlackBackend",
    "LocalConnectorBackend",
    "PathGmailBackend",
    "PathCalendarBackend",
]
