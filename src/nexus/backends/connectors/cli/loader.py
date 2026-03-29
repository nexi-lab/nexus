"""Declarative YAML config loader for CLI connectors.

Loads CLIConnectorConfig from YAML files, validates at load time,
and creates configured PathCLIBackend instances.

Phase 2 deliverable (Issue #3148, Decision #12A).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from nexus.backends.connectors.cli.config import CLIConnectorConfig

logger = logging.getLogger(__name__)


def load_connector_config(path: str | Path) -> CLIConnectorConfig:
    """Load and validate a CLI connector config from a YAML file.

    Args:
        path: Path to the YAML config file.

    Returns:
        Validated CLIConnectorConfig.

    Raises:
        FileNotFoundError: If the config file doesn't exist.
        yaml.YAMLError: If the YAML is malformed.
        pydantic.ValidationError: If the config is invalid.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Connector config not found: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        msg = f"Expected YAML mapping in {path}, got {type(raw).__name__}"
        raise ValueError(msg)

    # Support both root-level and nested "connector:" key
    config_data = raw.get("connector", raw)

    return CLIConnectorConfig.model_validate(config_data)


def load_all_configs(
    config_dir: str | Path,
) -> dict[str, CLIConnectorConfig]:
    """Load all CLI connector configs from a directory.

    Scans for ``*.yaml`` and ``*.yml`` files, validates each, and returns
    a mapping of connector name (filename stem) to config.

    Invalid configs are logged as warnings and skipped.

    Args:
        config_dir: Directory containing connector YAML configs.

    Returns:
        Mapping of connector name to validated config.
    """
    config_dir = Path(config_dir)
    if not config_dir.is_dir():
        logger.debug("Connector config directory not found: %s", config_dir)
        return {}

    configs: dict[str, CLIConnectorConfig] = {}

    for path in sorted(config_dir.glob("*.y*ml")):
        if not path.is_file():
            continue
        name = path.stem
        try:
            configs[name] = load_connector_config(path)
            logger.info("Loaded connector config: %s from %s", name, path)
        except Exception:
            logger.warning("Failed to load connector config %s", path, exc_info=True)

    return configs


def create_connector_from_yaml(
    config: CLIConnectorConfig,
    token_manager_db: str | None = None,
) -> Any:
    """Create a PathCLIBackend instance from a validated config.

    Args:
        config: Validated connector configuration.
        token_manager_db: Database URL for TokenManager (optional).

    Returns:
        Configured PathCLIBackend instance.
    """
    from nexus.backends.connectors.cli.base import PathCLIBackend

    connector = PathCLIBackend(
        config=config,
        token_manager_db=token_manager_db,
    )

    # Apply config to connector class attributes
    connector.SKILL_NAME = config.service

    # Build SCHEMAS from config schema references
    schemas: dict[str, Any] = {}
    for write_op in config.write:
        try:
            schema_class = _import_schema(write_op.schema_ref)
            schemas[write_op.operation] = schema_class
        except Exception:
            logger.warning(
                "Failed to import schema %s for operation %s",
                write_op.schema_ref,
                write_op.operation,
                exc_info=True,
            )
    connector.SCHEMAS = schemas

    # Build OPERATION_TRAITS from config
    from nexus.backends.connectors.base import ConfirmLevel, OpTraits, Reversibility

    traits: dict[str, OpTraits] = {}
    for write_op in config.write:
        traits[write_op.operation] = OpTraits(
            reversibility=Reversibility(write_op.traits.get("reversibility", "full")),
            confirm=ConfirmLevel(write_op.traits.get("confirm", "intent")),
        )
    connector.OPERATION_TRAITS = traits

    return connector


def create_connector_class_from_yaml(
    name: str,
    config: CLIConnectorConfig,
) -> type:
    """Create a dedicated PathCLIBackend subclass with baked-in config.

    Unlike ``create_connector_from_yaml`` which returns an instance with
    instance-level attribute overrides, this creates a proper subclass
    with class-level SCHEMAS, OPERATION_TRAITS, etc. so that
    ``ConnectorRegistry.register()`` can later instantiate it via
    ``BackendFactory.create()`` without losing the configuration.

    Args:
        name: Connector name (used for class name).
        config: Validated connector configuration.

    Returns:
        A new PathCLIBackend subclass with config baked into class attributes.
    """
    from nexus.backends.connectors.base import ConfirmLevel, OpTraits, Reversibility
    from nexus.backends.connectors.cli.base import PathCLIBackend

    # Build SCHEMAS from config schema references
    schemas: dict[str, Any] = {}
    for write_op in config.write:
        try:
            schema_class = _import_schema(write_op.schema_ref)
            schemas[write_op.operation] = schema_class
        except Exception:
            logger.warning(
                "Failed to import schema %s for operation %s",
                write_op.schema_ref,
                write_op.operation,
                exc_info=True,
            )

    # Build OPERATION_TRAITS from config
    traits: dict[str, OpTraits] = {}
    for write_op in config.write:
        traits[write_op.operation] = OpTraits(
            reversibility=Reversibility(write_op.traits.get("reversibility", "full")),
            confirm=ConfirmLevel(write_op.traits.get("confirm", "intent")),
        )

    # Dynamically create a subclass with the config baked in.
    # _DEFAULT_CONFIG is picked up by PathCLIBackend.__init__ as fallback
    # when no explicit config= kwarg is provided.
    cls_name = f"PathCLIBackend_{name.replace('-', '_').title()}"
    connector_cls: type = type(
        cls_name,
        (PathCLIBackend,),
        {
            "SKILL_NAME": config.service,
            "CLI_NAME": config.cli,
            "CLI_SERVICE": config.service,
            "SCHEMAS": schemas,
            "OPERATION_TRAITS": traits,
            "_DEFAULT_CONFIG": config,
        },
    )

    return connector_cls


def _import_schema(dotted_path: str) -> type:
    """Import a Pydantic schema class from a dotted path.

    Args:
        dotted_path: e.g., "nexus.connectors.gmail.schemas.SendEmailSchema"

    Returns:
        The imported class.
    """
    module_path, _, class_name = dotted_path.rpartition(".")
    if not module_path:
        msg = f"Invalid schema path: {dotted_path}"
        raise ImportError(msg)

    import importlib

    module = importlib.import_module(module_path)
    cls: type = getattr(module, class_name)
    return cls
