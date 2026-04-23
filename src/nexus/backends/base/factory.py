"""Centralized backend factory (Issue #1601, #2362).

Replaces duplicated if/elif chains in mount_service
and cli/utils with a single factory that uses ConnectorRegistry.

All registered connectors (including ``local``, ``passthrough``, and
all OAuth/cloud connectors) are created through the registry's config
mapping, which translates external config keys to constructor params.

``BackendFactory.wrap()`` adds a single wrapper layer to an existing
backend. Chain explicitly::

    wrapped = BackendFactory.wrap(
        BackendFactory.wrap(base, "encrypt", {"key": k}),
        "compress",
    )

Usage:
    >>> from nexus.backends.base.factory import BackendFactory
    >>> backend = BackendFactory.create("cas_local", {"data_dir": "/path"})
    >>> backend = BackendFactory.create("path_gcs", config, record_store=rs)
    >>> wrapped = BackendFactory.wrap(backend, "compress")
"""

import functools
import inspect
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.backends.base.backend import Backend

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
        (e.g., ``record_store``) are passed directly to the constructor
        only if the constructor accepts them.

        Args:
            backend_type: Backend type identifier (e.g., "cas_local", "path_gcs")
            config: Backend configuration dict with external config keys
            **extra_kwargs: Additional constructor kwargs not in config
                (e.g., record_store, metadata_store)

        Returns:
            Instantiated Backend

        Raises:
            RuntimeError: If ``backend_type`` is not registered, or if it
                is registered as a manifest placeholder whose module failed
                to import (deps satisfied but class never bound).
            TypeError: If required constructor args are missing
            MissingDependencyError: If any of the connector's runtime_deps are unmet
        """
        from nexus.backends.base.registry import (
            ConnectorRegistry,
            _ensure_optional_backends_registered,
        )
        from nexus.backends.base.runtime_deps import check_runtime_deps
        from nexus.contracts.exceptions import MissingDependencyError

        _ensure_optional_backends_registered()

        try:
            info = ConnectorRegistry.get_info(backend_type)
        except KeyError:
            raise RuntimeError(f"Unsupported backend type: {backend_type}") from None

        missing = check_runtime_deps(info.runtime_deps)
        if missing:
            raise MissingDependencyError(backend=backend_type, missing=missing)

        if info.connector_class is None:
            # Placeholder still unbound even though deps are now
            # satisfied. ``_register_optional_backends()`` is one-shot
            # per process — if the user just installed the missing
            # package, the placeholder will stay unbound until
            # restart. Attempt a targeted re-import of the manifest
            # module so the decorator runs and binds the class; then
            # re-read info. This lets "install dep → retry mount"
            # succeed in the same process.
            expected_path = info.expected_module_path
            rebind_error: BaseException | None = None
            if expected_path:
                import importlib

                try:
                    importlib.import_module(expected_path)
                except Exception as e:
                    # Catch Exception (not just ImportError) so a
                    # SyntaxError / RuntimeError surfaced during the
                    # targeted re-import does NOT escape as an
                    # uncontrolled exception and break per-connector
                    # failure containment. The controlled RuntimeError
                    # below chains the captured cause — including dep
                    # races (ImportError) and hard module bugs alike.
                    rebind_error = e
                info = ConnectorRegistry.get_info(backend_type)

            if info.connector_class is None:
                # Prefer the phase-2 failure captured at registration
                # time (recorded by record_import_failure for non-dep
                # errors like SyntaxError) — that IS the root cause.
                # The retry's ImportError only surfaces when we have
                # no captured history, since a fresh import failure
                # here is often a secondary effect of the earlier one.
                detail = info.import_error or (str(rebind_error) if rebind_error else None)
                base_msg = (
                    f"Connector '{backend_type}' is declared in the "
                    f"manifest but its module failed to import"
                )
                if detail:
                    base_msg = f"{base_msg}: {detail}"
                restart_hint = (
                    " — if the dependency was just installed, "
                    "restarting the process will clear any cached "
                    "import state."
                )
                if rebind_error is not None:
                    raise RuntimeError(base_msg + restart_hint) from rebind_error
                raise RuntimeError(base_msg + restart_hint)

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

    @staticmethod
    def wrap(
        inner: "Backend", wrapper_type: str, config: dict[str, Any] | None = None
    ) -> "Backend":
        """Wrap a backend with a single wrapper layer.

        Chain explicitly::

            wrapped = BackendFactory.wrap(
                BackendFactory.wrap(base, "encrypt", {"key": key}),
                "compress",
            )

        Args:
            inner: The backend to wrap.
            wrapper_type: One of "cache", "compress", "encrypt", "logging".
            config: Optional wrapper-specific configuration dict.

        Returns:
            A new Backend wrapping ``inner``.

        Raises:
            ValueError: If ``wrapper_type`` is not recognized.
        """
        cfg = config or {}
        result: Backend

        match wrapper_type:
            case "compress" | "compressed":
                from nexus.backends.wrappers.compressed import (
                    CompressedStorage,
                    CompressedStorageConfig,
                )

                compress_cfg = CompressedStorageConfig(**cfg) if cfg else None
                result = CompressedStorage(inner=inner, config=compress_cfg)
            case "encrypt" | "encrypted":
                from nexus.backends.wrappers.encrypted import (
                    EncryptedStorage,
                    EncryptedStorageConfig,
                )

                if not cfg:
                    raise ValueError("EncryptedStorage requires config with 'key' (32 bytes)")
                encrypt_cfg = EncryptedStorageConfig(**cfg)
                result = EncryptedStorage(inner=inner, config=encrypt_cfg)
            case "logging" | "log":
                from nexus.backends.wrappers.logging import LoggingBackendWrapper

                result = LoggingBackendWrapper(inner=inner)
            case _:
                raise ValueError(
                    f"Unknown wrapper type: {wrapper_type!r}. "
                    f"Known types: cache, compress, encrypt, logging"
                )

        logger.info(
            "Wrapped backend: %s",
            result.describe(),
        )
        return result
