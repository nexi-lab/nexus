"""Exception types for the extension metadata layer."""

from __future__ import annotations


class ExtensionError(Exception):
    """Base class for all extension-layer errors."""


class ManifestValidationError(ExtensionError):
    """Raised when a manifest fails Pydantic validation.

    Carries the source path so the user can locate the bad declaration.
    """

    def __init__(self, source: str, detail: str) -> None:
        super().__init__(f"Invalid manifest at {source}: {detail}")
        self.source = source
        self.detail = detail


class DuplicateManifestError(ExtensionError):
    """Raised when the same (kind, name) pair is declared twice from one source."""

    def __init__(self, kind: str, name: str, sources: tuple[str, ...]) -> None:
        super().__init__(f"Duplicate manifest for {kind}/{name} declared by: {', '.join(sources)}")
        self.kind = kind
        self.name = name
        self.sources = sources


class ReservedNameError(ExtensionError):
    """Raised when a manifest declares a reserved name."""

    def __init__(self, name: str, pattern: str) -> None:
        super().__init__(f"Manifest name '{name}' matches reserved pattern: {pattern}")
        self.name = name
        self.pattern = pattern


class IndexCorruptError(ExtensionError):
    """Raised when extensions.json is unreadable or malformed."""


class FactoryResolutionError(ExtensionError):
    """Raised when the factory callable named in a manifest can't be resolved."""

    def __init__(self, manifest_name: str, module: str, factory: str, detail: str) -> None:
        super().__init__(
            f"Cannot resolve factory '{factory}' for manifest '{manifest_name}' "
            f"in module '{module}': {detail}"
        )
        self.manifest_name = manifest_name
        self.module = module
        self.factory = factory
        self.detail = detail
