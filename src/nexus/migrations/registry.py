"""Migration registry for tracking and executing version migrations.

This module provides the infrastructure for registering, discovering, and
executing migrations between Nexus versions. It supports:
- Version-to-version migration steps
- Migration path finding (A -> B -> C)
- Rollback support with optional rollback functions
- Destructive operation warnings
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.migrations.version_manager import MigrationContext, MigrationResult


@dataclass
class MigrationStep:
    """Definition of a single migration step between versions.

    Attributes:
        from_version: Source version (e.g., "0.5.0")
        to_version: Target version (e.g., "0.6.0")
        name: Short identifier for this migration
        description: Human-readable description of what this migration does
        migrate_fn: Function to execute the migration
        rollback_fn: Optional function to rollback the migration
        requires_backup: Whether backup is required before this migration
        is_destructive: Whether this migration may cause data loss
        alembic_revision: Optional Alembic revision to apply (if schema migration)
    """

    from_version: str
    to_version: str
    name: str
    description: str
    migrate_fn: Callable[[MigrationContext], MigrationResult]
    rollback_fn: Callable[[MigrationContext], MigrationResult] | None = None
    requires_backup: bool = True
    is_destructive: bool = False
    alembic_revision: str | None = None


@dataclass
class MigrationPath:
    """A complete path from one version to another.

    Attributes:
        from_version: Starting version
        to_version: Target version
        steps: Ordered list of migration steps to execute
        total_requires_backup: Whether any step requires backup
        has_destructive_steps: Whether any step is destructive
    """

    from_version: str
    to_version: str
    steps: list[MigrationStep] = field(default_factory=list)

    @property
    def total_requires_backup(self) -> bool:
        """Check if any step in the path requires backup."""
        return any(step.requires_backup for step in self.steps)

    @property
    def has_destructive_steps(self) -> bool:
        """Check if any step in the path is destructive."""
        return any(step.is_destructive for step in self.steps)

    @property
    def all_rollbackable(self) -> bool:
        """Check if all steps have rollback functions."""
        return all(step.rollback_fn is not None for step in self.steps)


class MigrationRegistry:
    """Registry for all available migrations.

    The registry maintains a graph of migration steps between versions,
    allowing path finding for multi-step upgrades.

    Example:
        registry = MigrationRegistry()
        registry.register(MigrationStep(
            from_version="0.5.0",
            to_version="0.6.0",
            name="add_rebac_tables",
            description="Add ReBAC permission tables",
            migrate_fn=migrate_0_5_to_0_6,
            rollback_fn=rollback_0_6_to_0_5,
        ))

        path = registry.get_migration_path("0.5.0", "0.7.0")
        for step in path.steps:
            result = step.migrate_fn(context)
    """

    def __init__(self) -> None:
        """Initialize empty migration registry."""
        self._migrations: dict[tuple[str, str], MigrationStep] = {}
        self._versions: set[str] = set()

    def register(self, step: MigrationStep) -> None:
        """Register a migration step.

        Args:
            step: The migration step to register

        Raises:
            ValueError: If a migration for this version pair already exists
        """
        key = (step.from_version, step.to_version)
        if key in self._migrations:
            raise ValueError(
                f"Migration from {step.from_version} to {step.to_version} already registered"
            )

        self._migrations[key] = step
        self._versions.add(step.from_version)
        self._versions.add(step.to_version)

    def get_migration(self, from_version: str, to_version: str) -> MigrationStep | None:
        """Get a direct migration step between two versions.

        Args:
            from_version: Source version
            to_version: Target version

        Returns:
            The migration step if it exists, None otherwise
        """
        return self._migrations.get((from_version, to_version))

    def get_migration_path(self, from_version: str, to_version: str) -> MigrationPath | None:
        """Find a path of migrations from one version to another.

        Uses BFS to find the shortest path through the version graph.

        Args:
            from_version: Starting version
            to_version: Target version

        Returns:
            MigrationPath if a path exists, None otherwise
        """
        if from_version == to_version:
            return MigrationPath(from_version=from_version, to_version=to_version)

        # Direct migration exists
        direct = self.get_migration(from_version, to_version)
        if direct:
            return MigrationPath(
                from_version=from_version,
                to_version=to_version,
                steps=[direct],
            )

        # BFS to find shortest path
        from collections import deque

        queue: deque[tuple[str, list[MigrationStep]]] = deque([(from_version, [])])
        visited: set[str] = {from_version}

        while queue:
            current_version, path = queue.popleft()

            # Find all migrations from current version
            for (src, dst), step in self._migrations.items():
                if src == current_version and dst not in visited:
                    new_path = path + [step]

                    if dst == to_version:
                        return MigrationPath(
                            from_version=from_version,
                            to_version=to_version,
                            steps=new_path,
                        )

                    visited.add(dst)
                    queue.append((dst, new_path))

        return None

    def validate_migration_path(self, from_version: str, to_version: str) -> bool:
        """Check if a valid migration path exists.

        Args:
            from_version: Starting version
            to_version: Target version

        Returns:
            True if a path exists, False otherwise
        """
        return self.get_migration_path(from_version, to_version) is not None

    def get_available_versions(self) -> list[str]:
        """Get all versions known to the registry.

        Returns:
            Sorted list of version strings
        """
        return sorted(self._versions, key=_parse_version)

    def get_upgrades_from(self, version: str) -> list[str]:
        """Get all versions that can be directly upgraded to from a version.

        Args:
            version: The source version

        Returns:
            List of target versions
        """
        return [dst for (src, dst) in self._migrations if src == version]

    def get_downgrades_from(self, version: str) -> list[str]:
        """Get all versions that can be directly downgraded to from a version.

        Only returns versions where the migration has a rollback function.

        Args:
            version: The source version

        Returns:
            List of target versions with rollback support
        """
        result = []
        for (src, dst), step in self._migrations.items():
            if dst == version and step.rollback_fn is not None:
                result.append(src)
        return result


def _parse_version(version: str) -> tuple[int, ...]:
    """Parse version string into comparable tuple.

    Args:
        version: Version string (e.g., "0.5.0", "1.0.0-beta")

    Returns:
        Tuple of integers for comparison
    """
    # Strip any pre-release suffix
    base_version = version.split("-")[0]
    try:
        return tuple(int(x) for x in base_version.split("."))
    except ValueError:
        return (0, 0, 0)


# Global registry instance
_global_registry: MigrationRegistry | None = None


def get_registry() -> MigrationRegistry:
    """Get the global migration registry.

    Creates the registry and loads built-in migrations on first access.

    Returns:
        The global MigrationRegistry instance
    """
    global _global_registry
    if _global_registry is None:
        _global_registry = MigrationRegistry()
        _register_builtin_migrations(_global_registry)
    return _global_registry


def _register_builtin_migrations(registry: MigrationRegistry) -> None:
    """Register all built-in migrations.

    This function registers the standard migration steps between
    Nexus versions. Custom migrations can be registered separately.

    Args:
        registry: The registry to populate
    """
    # Migrations will be added as they are implemented
    # Example structure:
    #
    # from nexus.migrations.steps import migrate_0_5_to_0_6
    # registry.register(MigrationStep(
    #     from_version="0.5.0",
    #     to_version="0.6.0",
    #     name="add_rebac_v2",
    #     description="Upgrade ReBAC schema to v2",
    #     migrate_fn=migrate_0_5_to_0_6.upgrade,
    #     rollback_fn=migrate_0_5_to_0_6.downgrade,
    # ))
    pass
