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

import inspect
import logging
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from nexus.backends.base.runtime_deps import (
    BinaryDep,
    PythonDep,
    RuntimeDep,
    ServiceDep,
)
from nexus.contracts.backend_features import BackendFeature
from nexus.lib.registry import BaseRegistry

if TYPE_CHECKING:
    from nexus.backends._manifest import ConnectorManifestEntry
    from nexus.backends.base.backend import Backend


# --- Capability-to-Protocol mapping ---
# Used for registration-time validation: if a backend claims a capability
# that maps to a Protocol, we verify the class has the required methods.


def get_capability_protocols() -> dict[BackendFeature, type]:
    """Get capability-to-Protocol mapping for registration-time validation.

    Returns:
        Dictionary mapping capabilities to their Protocol classes.
        Only capabilities that have a corresponding Protocol are included.
    """
    from nexus.core.protocols.connector import (
        BatchContentProtocol,
        DirectoryListingProtocol,
        OAuthCapableProtocol,
        StreamingProtocol,
    )

    return {
        BackendFeature.STREAMING: StreamingProtocol,
        BackendFeature.BATCH_CONTENT: BatchContentProtocol,
        BackendFeature.DIRECTORY_LISTING: DirectoryListingProtocol,
        BackendFeature.OAUTH: OAuthCapableProtocol,
    }


logger = logging.getLogger(__name__)

# Members required by ConnectorProtocol (Issue #1703).
# Used for registration-time conformance validation.  Cannot use
# issubclass(cls, ConnectorProtocol) because Python's runtime_checkable
# doesn't support issubclass() on Protocols with @property members.
_CONNECTOR_PROTOCOL_MEMBERS: frozenset[str] = frozenset(
    {
        # ContentStoreProtocol
        "name",
        "write_content",
        "read_content",
        "delete_content",
        "content_exists",
        "get_content_size",
        # DirectoryOpsProtocol
        "mkdir",
        "rmdir",
        "is_directory",
        # ConnectorProtocol (connection lifecycle — connect/disconnect deleted #1811)
        "check_connection",
        # ConnectorProtocol (capability flags)
        "is_connected",
        "has_root_path",
        # CapabilityAwareProtocol (Issue #2069)
        "backend_features",
        "has_feature",
    }
)


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

    connector_class: "type[Backend] | None"
    """The connector class, or None if this is a manifest-registered
    placeholder whose module has not yet been imported (or whose import
    failed). ``BackendFactory.create()`` raises ``MissingDependencyError``
    or a clear ``RuntimeError`` when attempting to mount a placeholder
    whose deps are satisfied."""

    description: str = ""
    """Human-readable description of the connector."""

    category: str = "storage"
    """Category for grouping (e.g., 'storage', 'api', 'database')."""

    runtime_deps: tuple[RuntimeDep, ...] = ()
    """Typed runtime dependencies (Issue #3830).

    Populated at registration from either ``@register_connector(runtime_deps=...)``
    or the class attribute ``RUNTIME_DEPS``. Checked by
    :meth:`nexus.backends.base.factory.BackendFactory.create` before
    instantiation.
    """

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

    backend_features: frozenset[BackendFeature] = field(default_factory=frozenset)
    """Capabilities declared by this connector (Issue #2069)."""

    @property
    def connection_args(self) -> dict[str, ConnectionArg]:
        """Get CONNECTION_ARGS from the connector class if defined.

        Returns:
            Dictionary of argument name to ConnectionArg, or empty dict if not defined.
        """
        return getattr(self.connector_class, "CONNECTION_ARGS", {})

    @property
    def requires(self) -> list[str]:
        """Deprecated — derived from ``runtime_deps``.

        Returns the module names of every :class:`PythonDep`.  New code
        should read ``runtime_deps`` directly; this property exists so that
        current callers (``cli/commands/connectors.py``,
        ``server/api/v2/routers/connectors.py``, tests) keep working for
        one release.  Removal is tracked as follow-up A.2 of Issue #3830.

        Non-Python dependencies (``BinaryDep``, ``ServiceDep``) are *not*
        represented in this view — new callers should read ``runtime_deps``
        directly to see the full dep set.
        """
        return [d.module for d in self.runtime_deps if isinstance(d, PythonDep)]

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


def derive_config_mapping(connector_class: "type[Backend]") -> dict[str, str]:
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
        ... class AzureBlobConnector(PathAddressingEngine):
        ...     pass
        ...
        >>> ConnectorRegistry.get("azure_blob")
        <class 'AzureBlobConnector'>
        >>> ConnectorRegistry.list_available()
        ['azure_blob', 'gcs_connector', 's3_connector', ...]
    """

    _base: BaseRegistry[ConnectorInfo] = BaseRegistry("connectors")

    @classmethod
    def register_placeholder(cls, entry: "ConnectorManifestEntry") -> None:
        """Register a manifest-sourced placeholder ConnectorInfo.

        Called during Phase 1 of ``_register_optional_backends()``, before
        any connector module is imported. The placeholder carries the
        manifest's metadata and runtime_deps but has ``connector_class=None``.

        If the connector module imports successfully later, its
        ``@register_connector("name")`` call hits the placeholder-binding
        path in :meth:`register` and attaches the real class (preserving
        manifest metadata). If the import fails, the placeholder remains
        — ``BackendFactory.create()`` will raise ``MissingDependencyError``
        with the manifest's install hints.

        If a prior direct import already bound the class (e.g., test code
        running ``from nexus.backends.storage.cas_local import CASLocalBackend``
        before ``_register_optional_backends()``), preserve the class
        binding and backfill manifest metadata on top. This keeps the
        manifest authoritative for metadata without losing the already-bound
        class.
        """
        existing = cls._base.get(entry.name)
        if existing is not None and existing.connector_class is not None:
            # Already bound by a prior direct import. Preserve the class
            # and its derived fields; backfill manifest metadata.
            merged = ConnectorInfo(
                name=entry.name,
                connector_class=existing.connector_class,
                description=entry.description,
                category=entry.category,
                user_scoped=existing.user_scoped,
                config_mapping=existing.config_mapping,
                service_name=entry.service_name,
                backend_features=existing.backend_features,
                runtime_deps=entry.runtime_deps,
            )
            cls._base.register(entry.name, merged, allow_overwrite=True)
            return

        info = ConnectorInfo(
            name=entry.name,
            connector_class=None,
            description=entry.description,
            category=entry.category,
            runtime_deps=entry.runtime_deps,
            service_name=entry.service_name,
        )
        cls._base.register(entry.name, info, allow_overwrite=True)

    @classmethod
    def register(
        cls,
        name: str,
        connector_class: "type[Backend]",
        description: str = "",
        category: str = "storage",
        requires: list[str] | None = None,  # noqa: ARG003 — accepted for API compat, ignored per spec §6
        service_name: str | None = None,
        runtime_deps: tuple[RuntimeDep, ...] | None = None,
    ) -> None:
        """Register a connector class.

        Args:
            name: Unique identifier for the connector
            connector_class: The connector class to register
            description: Human-readable description
            category: Category for grouping
            requires: **Deprecated** — list of pip-package names. Prefer
                ``runtime_deps`` with :class:`PythonDep` entries.
            service_name: Unified service name for service_map integration
            runtime_deps: Typed runtime dependencies (Issue #3830). Takes
                precedence over the class attribute ``RUNTIME_DEPS``.

        Raises:
            ValueError: If a connector with the same name is already
                registered, if ``runtime_deps`` contains non-RuntimeDep
                entries, or if the connector class does not satisfy
                ConnectorProtocol.
        """
        # Validate ConnectorProtocol conformance (Issue #1703).
        missing = [m for m in _CONNECTOR_PROTOCOL_MEMBERS if not hasattr(connector_class, m)]
        if missing:
            raise ValueError(
                f"Connector '{name}' ({connector_class.__name__}) does not satisfy "
                f"ConnectorProtocol. Missing members: {', '.join(sorted(missing))}"
            )

        existing = cls._base.get(name)
        if existing is not None:
            if existing.connector_class is None:
                # Placeholder-binding path. The manifest (via
                # register_placeholder) already set runtime_deps,
                # description, category, service_name. We attach
                # the real class and derive class-scoped fields
                # (user_scoped, config_mapping, backend_features).
                # Decorator kwargs other than `name` are IGNORED
                # here — manifest is the single source of truth for
                # built-in connectors. If the caller passed any such
                # kwargs, emit a UserWarning pointing them at the
                # manifest.
                decorator_provided_metadata = (
                    (description and description != existing.description)
                    or (category != existing.category)
                    or bool(requires)
                    or (service_name is not None and service_name != existing.service_name)
                    or (runtime_deps is not None and tuple(runtime_deps) != existing.runtime_deps)
                )
                if decorator_provided_metadata:
                    warnings.warn(
                        f"@register_connector('{name}', ...): metadata kwargs "
                        f"ignored because '{name}' is declared in the manifest "
                        f"(src/nexus/backends/_manifest.py). Edit the manifest "
                        f"to change metadata for built-in connectors.",
                        UserWarning,
                        stacklevel=3,
                    )

                user_scoped = getattr(connector_class, "user_scoped", False)
                if isinstance(user_scoped, property):
                    user_scoped = False

                bound = ConnectorInfo(
                    name=name,
                    connector_class=connector_class,
                    description=existing.description,
                    category=existing.category,
                    user_scoped=user_scoped,
                    config_mapping=derive_config_mapping(connector_class),
                    service_name=existing.service_name,
                    backend_features=getattr(connector_class, "_BACKEND_FEATURES", frozenset()),
                    runtime_deps=existing.runtime_deps,
                )
                cls._base.register(name, bound, allow_overwrite=True)
                return

            if existing.connector_class is not connector_class:
                raise ValueError(
                    f"Connector '{name}' is already registered to "
                    f"{existing.connector_class.__name__}. "
                    f"Cannot register {connector_class.__name__}."
                )
            # Same class; idempotent re-registration.
            return

        user_scoped = getattr(connector_class, "user_scoped", False)
        if isinstance(user_scoped, property):
            user_scoped = False

        config_mapping = derive_config_mapping(connector_class)

        backend_features: frozenset[BackendFeature] = getattr(
            connector_class, "_BACKEND_FEATURES", frozenset()
        )

        # Resolve runtime_deps: decorator arg wins, else class attr, else ().
        # Legacy ``requires=`` is ignored (not translated) — PyPI package
        # names are not always valid importable module names
        # (``google-cloud-storage`` vs. ``google.cloud.storage``).
        # Callers are expected to migrate to ``runtime_deps=`` per the
        # DeprecationWarning emitted above. See Issue #3830 spec §6.
        class_attr_deps = getattr(connector_class, "RUNTIME_DEPS", None)
        resolved_deps: tuple[RuntimeDep, ...]
        if runtime_deps is not None:
            if class_attr_deps is not None and tuple(class_attr_deps) != tuple(runtime_deps):
                warnings.warn(
                    f"Connector '{name}': runtime_deps= decorator arg overrides "
                    f"class attribute RUNTIME_DEPS.",
                    UserWarning,
                    stacklevel=3,
                )
            resolved_deps = tuple(runtime_deps)
        elif class_attr_deps is not None:
            resolved_deps = tuple(class_attr_deps)
        else:
            resolved_deps = ()

        bad = [d for d in resolved_deps if not isinstance(d, (PythonDep, BinaryDep, ServiceDep))]
        if bad:
            raise ValueError(
                f"Connector '{name}': runtime_deps / RUNTIME_DEPS entries must be "
                f"PythonDep / BinaryDep / ServiceDep, got: {bad!r}"
            )

        info = ConnectorInfo(
            name=name,
            connector_class=connector_class,
            description=description,
            category=category,
            user_scoped=user_scoped,
            config_mapping=config_mapping,
            service_name=service_name,
            backend_features=backend_features,
            runtime_deps=resolved_deps,
        )
        cls._base.register(name, info, allow_overwrite=True)

    @classmethod
    def get(cls, name: str) -> "type[Backend]":
        """Get a connector class by name.

        Args:
            name: Connector identifier

        Returns:
            The connector class

        Raises:
            KeyError: If connector is not found
        """
        try:
            info = cls._base.get_or_raise(name)
        except KeyError:
            available = ", ".join(cls._base.list_names())
            raise KeyError(f"Unknown connector '{name}'. Available: {available}") from None
        if info.connector_class is None:
            raise KeyError(
                f"Connector '{name}' is registered as a manifest placeholder but "
                f"its module has not been imported (likely due to missing runtime "
                f"dependencies). Use BackendFactory.create() for a helpful "
                f"MissingDependencyError with install hints."
            )
        return info.connector_class

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
    def get_backend_features(cls, name: str) -> frozenset[BackendFeature]:
        """Get capabilities for a connector by name (Issue #2069).

        Args:
            name: Connector identifier

        Returns:
            frozenset of BackendFeature values

        Raises:
            KeyError: If connector is not found
        """
        return cls.get_info(name).backend_features

    @classmethod
    def list_by_feature(cls, cap: BackendFeature) -> list[ConnectorInfo]:
        """List all connectors with a specific capability (Issue #2069).

        Args:
            cap: Capability to filter by

        Returns:
            List of ConnectorInfo objects that declare the given capability
        """
        return [info for info in cls._base.list_all() if cap in info.backend_features]

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
    runtime_deps: tuple[RuntimeDep, ...] | None = None,
) -> "Callable[[type[Backend]], type[Backend]]":
    """Decorator to register a connector class.

    Args:
        name: Unique identifier for the connector
        description: Human-readable description
        category: Category for grouping
        requires: **Deprecated** — use ``runtime_deps=`` instead.
        service_name: Unified service name for service_map integration
        runtime_deps: Typed runtime deps (Issue #3830). Takes precedence
            over the class attribute ``RUNTIME_DEPS``.

    Example::

        @register_connector(
            "path_s3",
            runtime_deps=(PythonDep("boto3", extras=("s3",)),),
        )
        class PathS3Backend(PathAddressingEngine):
            ...
    """
    if requires:
        warnings.warn(
            "register_connector(requires=...) is deprecated; "
            "use runtime_deps=(PythonDep(...), ...) instead.",
            DeprecationWarning,
            stacklevel=2,
        )

    def decorator(cls: "type[Backend]") -> "type[Backend]":
        ConnectorRegistry.register(
            name=name,
            connector_class=cls,
            description=description,
            category=category,
            requires=requires,
            service_name=service_name,
            runtime_deps=runtime_deps,
        )
        return cls

    return decorator


def _ensure_optional_backends_registered() -> None:
    """Ensure optional backends are registered (lazy loading)."""
    from nexus.backends import _register_optional_backends

    _register_optional_backends()


def create_connector(name: str, **config: Any) -> "Backend":
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
        ...     "path_gcs",
        ...     bucket_name="my-bucket",
        ...     project_id="my-project"
        ... )
    """
    from nexus.backends.base.factory import BackendFactory

    return BackendFactory.create(name, config)


def create_connector_from_config(name: str, backend_config: dict[str, Any]) -> "Backend":
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
        ...     "path_gcs",
        ...     {"bucket": "my-bucket", "project_id": "my-project"}
        ... )
    """
    from nexus.backends.base.factory import BackendFactory

    return BackendFactory.create(name, backend_config)
