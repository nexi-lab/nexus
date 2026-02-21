"""Centralized backend factory (Issue #1601).

Replaces duplicated if/elif chains in mount_core_service, mount_service,
and cli/utils with a single factory that uses ConnectorRegistry.

All registered connectors (including ``local``, ``passthrough``, and
all OAuth/cloud connectors) are created through the registry's config
mapping, which translates external config keys to constructor params.

Usage:
    >>> from nexus.backends.factory import BackendFactory
    >>> backend = BackendFactory.create("local", {"data_dir": "/path"})
    >>> backend = BackendFactory.create("gcs_connector", config, session_factory=sf)
"""

import functools
import inspect
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.backends.backend import Backend

logger = logging.getLogger(__name__)


class BackendFactory:
    """Centralized factory for creating backend instances by type name.

    Uses ConnectorRegistry for all registered backends, mapping config
    dict keys to constructor params via each connector's CONNECTION_ARGS.
    """

    @staticmethod
    def create(backend_type: str, config: dict[str, Any], **extra_kwargs: Any) -> "Backend":
        """Create a backend instance by type name and config dict.

        Uses ConnectorRegistry for all registered connectors. Extra kwargs
        (e.g., ``session_factory``) are passed directly to the constructor
        only if the constructor accepts them.

        Args:
            backend_type: Backend type identifier (e.g., "local", "gcs_connector")
            config: Backend configuration dict with external config keys
            **extra_kwargs: Additional constructor kwargs not in config
                (e.g., session_factory, metadata_store)

        Returns:
            Instantiated Backend

        Raises:
            RuntimeError: If backend_type is not registered
            TypeError: If required constructor args are missing
        """
        from nexus.backends.registry import ConnectorRegistry, _ensure_optional_backends_registered

        _ensure_optional_backends_registered()

        try:
            info = ConnectorRegistry.get_info(backend_type)
        except KeyError:
            raise RuntimeError(f"Unsupported backend type: {backend_type}") from None
        connector_cls = info.connector_class
        mapping = info.config_mapping

        # Build constructor kwargs by mapping config keys to param names
        kwargs: dict[str, Any] = {}
        for config_key, param_name in mapping.items():
            if config_key in config:
                kwargs[param_name] = config[config_key]

        # Pass through any config keys that match param names directly
        for key, value in config.items():
            if key not in mapping and key not in kwargs:
                kwargs[key] = value

        # Only pass extra kwargs the constructor actually accepts
        if extra_kwargs:
            accepted, accepts_var_kw = BackendFactory._accepted_params(connector_cls)
            if accepts_var_kw:
                kwargs.update(extra_kwargs)
            else:
                for key, value in extra_kwargs.items():
                    if key in accepted:
                        kwargs[key] = value

        return connector_cls(**kwargs)

    @staticmethod
    @functools.lru_cache(maxsize=64)
    def _accepted_params(cls: type) -> tuple[frozenset[str], bool]:
        """Return (accepted_param_names, accepts_var_keyword) for a class."""
        sig = inspect.signature(cls.__init__)  # type: ignore[misc]
        accepts_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        return frozenset(sig.parameters.keys()) - frozenset({"self"}), accepts_var_kw
