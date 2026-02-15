"""Connector registry for dynamic backend loading and discovery.

This module provides a registry pattern for connectors, enabling:
- Dynamic plugin loading at import time via decorators
- Runtime discovery of available connectors
- CLI command for listing connectors (`nexus connectors list`)
- Cleaner factory pattern (lookup by name instead of if/elif)
- Standardized connection argument definitions

Usage:
    # Register a connector with CONNECTION_ARGS
    @register_connector("my_connector")
    class MyConnector(Backend):
        CONNECTION_ARGS = {
            'bucket_name': ConnectionArg(ArgType.STRING, 'Bucket name'),
            'secret_key': ConnectionArg(ArgType.SECRET, 'API secret', secret=True),
        }
        ...

    # Get a connector class by name
    connector_cls = ConnectorRegistry.get("my_connector")

    # List available connectors
    available = ConnectorRegistry.list_available()

    # Get connection args for a connector
    args = ConnectorRegistry.get_connection_args("my_connector")

Inspired by:
- MindsDB handler registry pattern (connection_args.py)
- n8n node discovery system and credentials separation
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from nexus.core.registry import BaseRegistry

if TYPE_CHECKING:
    from nexus.backends.backend import Backend

logger = logging.getLogger(__name__)


class ArgType(Enum):
    """Types for connection arguments.

    Used to indicate how arguments should be handled in UI/CLI and validation.
    """

    STRING = "string"
    """Regular string value."""

    SECRET = "secret"
    """Sensitive value that should be masked in logs/UI."""

    PASSWORD = "password"
    """Password field, never displayed after entry."""

    INTEGER = "integer"
    """Integer value."""

    BOOLEAN = "boolean"
    """Boolean flag."""

    PATH = "path"
    """File system path (validated for existence optionally)."""

    OAUTH = "oauth"
    """OAuth credential reference (handled by TokenManager)."""


@dataclass
class ConnectionArg:
    """Definition of a connection argument for a connector.

    This class describes a single configuration parameter that a connector
    accepts. It provides metadata for:
    - CLI help generation
    - UI form generation
    - Validation
    - Secret masking in logs

    Example:
        >>> ConnectionArg(
        ...     type=ArgType.STRING,
        ...     description="GCS bucket name",
        ...     required=True,
        ... )
    """

    type: ArgType
    """The type of this argument."""

    description: str
    """Human-readable description of this argument."""

    required: bool = True
    """Whether this argument is required."""

    default: Any = None
    """Default value if not provided."""

    secret: bool = False
    """Whether this value should be masked in logs/UI."""

    env_var: str | None = None
    """Environment variable to read from if not provided."""

    config_key: str | None = None
    """External config key that maps to this constructor param.

    When set, this is the key used in backend_config dicts (e.g., ``"bucket"``
    maps to constructor param ``bucket_name``).  When ``None``, the
    CONNECTION_ARGS dict key is used as both config key and param name
    (identity mapping).
    """

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        result = {
            "type": self.type.value,
            "description": self.description,
            "required": self.required,
            "default": self.default,
            "secret": self.secret,
            "env_var": self.env_var,
        }
        if self.config_key is not None:
            result["config_key"] = self.config_key
        return result


@dataclass
class ConnectorInfo:
    """Metadata about a registered connector."""

    name: str
    """Unique identifier for the connector (e.g., 'gcs_connector', 's3_connector')."""

    connector_class: type[Backend]
    """The connector class."""

    description: str = ""
    """Human-readable description of the connector."""

    category: str = "storage"
    """Category for grouping (e.g., 'storage', 'api', 'database')."""

    requires: list[str] = field(default_factory=list)
    """List of optional dependencies required by this connector."""

    user_scoped: bool = False
    """Whether this connector requires per-user OAuth credentials."""

    config_mapping: dict[str, str] = field(default_factory=dict)
    """Derived mapping from external config keys to constructor param names.

    Auto-populated at registration time by :func:`derive_config_mapping`.
    """

    service_name: str | None = None
    """Unified service name for service_map integration (e.g., 'gmail', 'google-drive').

    When set, service_map.py auto-derives the connector field from this registry,
    eliminating manual synchronization between ConnectorRegistry and SERVICE_REGISTRY.
    """

    @property
    def connection_args(self) -> dict[str, ConnectionArg]:
        """Get CONNECTION_ARGS from the connector class if defined.

        Returns:
            Dictionary of argument name to ConnectionArg, or empty dict if not defined.
        """
        return getattr(self.connector_class, "CONNECTION_ARGS", {})

    def get_required_args(self) -> list[str]:
        """Get names of required connection arguments.

        Returns:
            List of required argument names.
        """
        return [name for name, arg in self.connection_args.items() if arg.required]

    def get_secret_args(self) -> list[str]:
        """Get names of secret connection arguments.

        Returns:
            List of argument names that should be masked.
        """
        return [name for name, arg in self.connection_args.items() if arg.secret]


def derive_config_mapping(connector_class: type[Backend]) -> dict[str, str]:
    """Auto-derive config key -> constructor param mapping from CONNECTION_ARGS.

    For each ``(param_name, connection_arg)`` in ``CONNECTION_ARGS``:

    * If ``connection_arg.config_key`` is set: ``config_key -> param_name``
    * Otherwise: ``param_name -> param_name`` (identity mapping)

    Args:
        connector_class: A backend class with an optional ``CONNECTION_ARGS``
            class attribute.

    Returns:
        Mapping from external config keys to constructor parameter names.

    Raises:
        ValueError: If a mapped parameter name does not exist in the
            connector's ``__init__`` signature.
    """
    connection_args: dict[str, ConnectionArg] = getattr(connector_class, "CONNECTION_ARGS", {})
    if not connection_args:
        return {}

    # Validate against __init__ signature
    sig = inspect.signature(connector_class.__init__)
    valid_params = set(sig.parameters.keys()) - {"self"}

    mapping: dict[str, str] = {}
    for param_name, arg in connection_args.items():
        if param_name not in valid_params:
            raise ValueError(
                f"{connector_class.__name__}: CONNECTION_ARGS key '{param_name}' "
                f"does not match any __init__ parameter. "
                f"Valid params: {sorted(valid_params)}"
            )
        config_key = arg.config_key if arg.config_key is not None else param_name
        mapping[config_key] = param_name

    return mapping


class ConnectorRegistry:
    """Registry for dynamic connector loading and discovery.

    Delegates storage to a ``BaseRegistry[ConnectorInfo]`` instance while
    preserving the existing classmethod-based public API.

    Example:
        >>> @register_connector("azure_blob", description="Azure Blob Storage")
        ... class AzureBlobConnector(BaseBlobStorageConnector):
        ...     pass
        ...
        >>> ConnectorRegistry.get("azure_blob")
        <class 'AzureBlobConnector'>
        >>> ConnectorRegistry.list_available()
        ['azure_blob', 'gcs_connector', 's3_connector', ...]
    """

    _base: BaseRegistry[ConnectorInfo] = BaseRegistry("connectors")

    @classmethod
    def register(
        cls,
        name: str,
        connector_class: type[Backend],
        description: str = "",
        category: str = "storage",
        requires: list[str] | None = None,
        service_name: str | None = None,
    ) -> None:
        """Register a connector class.

        Args:
            name: Unique identifier for the connector
            connector_class: The connector class to register
            description: Human-readable description
            category: Category for grouping
            requires: List of optional dependencies
            service_name: Unified service name for service_map integration

        Raises:
            ValueError: If a connector with the same name is already registered
        """
        existing = cls._base.get(name)
        if existing is not None:
            if existing.connector_class is not connector_class:
                raise ValueError(
                    f"Connector '{name}' is already registered to {existing.connector_class.__name__}. "
                    f"Cannot register {connector_class.__name__}."
                )
            # Same class, skip duplicate registration (can happen with re-imports)
            return

        # Get user_scoped from class if it exists
        user_scoped = getattr(connector_class, "user_scoped", False)
        # Handle property descriptor
        if isinstance(user_scoped, property):
            # Default to False for property, will be checked at instance level
            user_scoped = False

        config_mapping = derive_config_mapping(connector_class)

        info = ConnectorInfo(
            name=name,
            connector_class=connector_class,
            description=description,
            category=category,
            requires=requires or [],
            user_scoped=user_scoped,
            config_mapping=config_mapping,
            service_name=service_name,
        )
        cls._base.register(name, info, allow_overwrite=True)

    @classmethod
    def get(cls, name: str) -> type[Backend]:
        """Get a connector class by name.

        Args:
            name: Connector identifier

        Returns:
            The connector class

        Raises:
            KeyError: If connector is not found
        """
        try:
            return cls._base.get_or_raise(name).connector_class
        except KeyError:
            available = ", ".join(cls._base.list_names())
            raise KeyError(f"Unknown connector '{name}'. Available: {available}") from None

    @classmethod
    def get_info(cls, name: str) -> ConnectorInfo:
        """Get connector info by name.

        Args:
            name: Connector identifier

        Returns:
            ConnectorInfo with metadata

        Raises:
            KeyError: If connector is not found
        """
        try:
            return cls._base.get_or_raise(name)
        except KeyError:
            available = ", ".join(cls._base.list_names())
            raise KeyError(f"Unknown connector '{name}'. Available: {available}") from None

    @classmethod
    def get_connection_args(cls, name: str) -> dict[str, ConnectionArg]:
        """Get connection arguments for a connector.

        Args:
            name: Connector identifier

        Returns:
            Dictionary of argument name to ConnectionArg

        Raises:
            KeyError: If connector is not found
        """
        return cls.get_info(name).connection_args

    @classmethod
    def list_available(cls) -> list[str]:
        """List all registered connector names.

        Returns:
            Sorted list of connector names
        """
        return cls._base.list_names()

    @classmethod
    def list_all(cls) -> list[ConnectorInfo]:
        """List all registered connectors with their metadata.

        Returns:
            List of ConnectorInfo objects, sorted by name
        """
        return cls._base.list_all()

    @classmethod
    def list_by_category(cls, category: str) -> list[ConnectorInfo]:
        """List connectors in a specific category.

        Args:
            category: Category to filter by

        Returns:
            List of ConnectorInfo objects in that category
        """
        return [info for info in cls._base.list_all() if info.category == category]

    @classmethod
    def is_registered(cls, name: str) -> bool:
        """Check if a connector is registered.

        Args:
            name: Connector identifier

        Returns:
            True if registered, False otherwise
        """
        return name in cls._base

    @classmethod
    def get_by_service_name(cls, service_name: str) -> ConnectorInfo | None:
        """Get connector info by unified service name.

        Args:
            service_name: Unified service name (e.g., 'gmail', 'google-drive')

        Returns:
            ConnectorInfo if found, None otherwise
        """
        for info in cls._base.list_all():
            if info.service_name == service_name:
                return info
        return None

    @classmethod
    def clear(cls) -> None:
        """Clear all registered connectors. Primarily for testing."""
        cls._base.clear()


def register_connector(
    name: str,
    description: str = "",
    category: str = "storage",
    requires: list[str] | None = None,
    service_name: str | None = None,
) -> Callable[[type[Backend]], type[Backend]]:
    """Decorator to register a connector class.

    Use this decorator on connector classes to automatically register them
    with the ConnectorRegistry at import time.

    Args:
        name: Unique identifier for the connector (e.g., 'gcs_connector')
        description: Human-readable description
        category: Category for grouping (default: 'storage')
        requires: List of optional dependencies (e.g., ['google-cloud-storage'])
        service_name: Unified service name for service_map integration
            (e.g., 'gmail', 'google-drive'). When set, service_map.py
            auto-derives the connector field from this registry.

    Returns:
        Decorator function

    Example:
        >>> @register_connector(
        ...     "azure_blob",
        ...     description="Azure Blob Storage connector",
        ...     requires=["azure-storage-blob"]
        ... )
        ... class AzureBlobConnector(BaseBlobStorageConnector):
        ...     pass
    """

    def decorator(cls: type[Backend]) -> type[Backend]:
        ConnectorRegistry.register(
            name=name,
            connector_class=cls,
            description=description,
            category=category,
            requires=requires,
            service_name=service_name,
        )
        return cls

    return decorator


def _ensure_optional_backends_registered() -> None:
    """Ensure optional backends are registered (lazy loading)."""
    from nexus.backends import _register_optional_backends

    _register_optional_backends()


def create_connector(name: str, **config: Any) -> Backend:
    """Factory function to create a connector instance by name.

    This is a convenience function that looks up the connector class
    and instantiates it with the provided configuration.

    Args:
        name: Connector identifier
        **config: Configuration parameters to pass to the connector

    Returns:
        Instantiated connector

    Raises:
        KeyError: If connector is not found

    Example:
        >>> backend = create_connector(
        ...     "gcs_connector",
        ...     bucket_name="my-bucket",
        ...     project_id="my-project"
        ... )
    """
    # Ensure optional backends are registered on first use
    _ensure_optional_backends_registered()
    connector_cls = ConnectorRegistry.get(name)
    return connector_cls(**config)


def create_connector_from_config(name: str, backend_config: dict[str, Any]) -> Backend:
    """Factory function to create a connector from a config dict.

    This maps config dict keys to constructor parameters using the
    registered config mappings.

    Args:
        name: Connector identifier
        backend_config: Configuration dict with backend-specific keys

    Returns:
        Instantiated connector

    Raises:
        KeyError: If connector is not found

    Example:
        >>> backend = create_connector_from_config(
        ...     "gcs_connector",
        ...     {"bucket": "my-bucket", "project_id": "my-project"}
        ... )
    """
    # Ensure optional backends are registered on first use
    _ensure_optional_backends_registered()
    info = ConnectorRegistry.get_info(name)
    connector_cls = info.connector_class

    # Get auto-derived config mapping from ConnectorInfo
    mapping = info.config_mapping

    # Build constructor kwargs by mapping config keys
    kwargs: dict[str, Any] = {}
    for config_key, param_name in mapping.items():
        if config_key in backend_config:
            kwargs[param_name] = backend_config[config_key]

    # Also pass through any keys that match parameter names directly
    # (for future extensibility without updating mappings)
    for key, value in backend_config.items():
        if key not in mapping and key not in kwargs:
            kwargs[key] = value

    return connector_cls(**kwargs)
