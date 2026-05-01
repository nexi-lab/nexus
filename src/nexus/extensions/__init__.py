"""Nexus unified extension metadata layer.

This package owns the manifest contract and discovery store shared by
plugins, connectors, and bricks. It MUST NOT import any extension impl
module — keeping that boundary lets introspection enumerate extensions
without triggering optional-dependency imports.
"""

from nexus.extensions.errors import (
    DuplicateManifestError,
    ExtensionError,
    FactoryResolutionError,
    IndexCorruptError,
    ManifestValidationError,
    ReservedNameError,
)
from nexus.extensions.manifest import (
    AnyManifest,
    BrickManifest,
    ConnectorManifest,
    ExtensionManifest,
    PluginManifest,
    RuntimeDep,
    parse_manifest,
)
from nexus.extensions.types import ArgType, ConnectionArg, Kind

__all__ = [
    "AnyManifest",
    "ArgType",
    "BrickManifest",
    "ConnectionArg",
    "ConnectorManifest",
    "DuplicateManifestError",
    "ExtensionError",
    "ExtensionManifest",
    "FactoryResolutionError",
    "IndexCorruptError",
    "Kind",
    "ManifestValidationError",
    "PluginManifest",
    "ReservedNameError",
    "RuntimeDep",
    "parse_manifest",
]
