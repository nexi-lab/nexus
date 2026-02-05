"""Configuration migrator for version upgrades.

This module handles migration of configuration files between
Nexus versions, including:
- Schema changes
- Renamed fields
- Deprecated options
- New required fields

Issue #165: Migration Tools & Upgrade Paths
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ConfigMigrationResult:
    """Result of configuration migration.

    Attributes:
        success: Whether migration completed successfully
        from_version: Source config version
        to_version: Target config version
        changes_made: List of changes applied
        warnings: Non-critical issues found
        errors: Critical errors that prevented migration
    """

    success: bool = True
    from_version: str = ""
    to_version: str = ""
    changes_made: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class ConfigMigrator:
    """Migrates configuration between Nexus versions.

    Handles:
    - Field renames and restructuring
    - Default value updates
    - Deprecated option removal
    - New required field addition

    Example:
        migrator = ConfigMigrator()
        result = migrator.migrate_config(old_config, "0.5.0", "0.6.0")
        if result.success:
            save_config(result.new_config)
    """

    def __init__(self) -> None:
        """Initialize config migrator."""
        # Registry of migration functions by version pair
        self._migrations: dict[tuple[str, str], Any] = {
            # Add migration functions as needed
            # ("0.5.0", "0.6.0"): self._migrate_0_5_to_0_6,
        }

    def migrate_config(
        self,
        config: dict[str, Any],
        from_version: str,
        to_version: str,
    ) -> tuple[dict[str, Any], ConfigMigrationResult]:
        """Migrate configuration from one version to another.

        Args:
            config: Configuration dictionary to migrate
            from_version: Source version
            to_version: Target version

        Returns:
            Tuple of (migrated_config, result)
        """
        result = ConfigMigrationResult(
            from_version=from_version,
            to_version=to_version,
        )

        # Make a copy to avoid modifying original
        new_config = dict(config)

        # Find migration path
        migration_fn = self._migrations.get((from_version, to_version))

        if migration_fn:
            try:
                new_config = migration_fn(new_config, result)
            except Exception as e:
                result.success = False
                result.errors.append(f"Migration failed: {e}")
                return config, result
        else:
            # No specific migration, apply general transformations
            new_config = self._apply_general_migrations(
                new_config, from_version, to_version, result
            )

        return new_config, result

    def validate_config(self, config: dict[str, Any], version: str) -> list[str]:
        """Validate configuration for a specific version.

        Args:
            config: Configuration to validate
            version: Target version to validate against

        Returns:
            List of validation error messages (empty if valid)
        """
        errors: list[str] = []

        # Common validations
        if "data_dir" in config and not config["data_dir"]:
            errors.append("data_dir cannot be empty")

        if "backend" in config:
            valid_backends = {"local", "gcs", "s3"}
            if config["backend"] not in valid_backends:
                errors.append(
                    f"Invalid backend: {config['backend']}. Must be one of: {valid_backends}"
                )

        # Version-specific validations
        if version >= "0.6.0":
            # ReBAC is required from v0.6.0
            pass

        return errors

    def get_config_version(self, config: dict[str, Any]) -> str | None:
        """Detect config version from content.

        Args:
            config: Configuration dictionary

        Returns:
            Detected version string, or None if unknown
        """
        # Check for version field
        if "version" in config:
            return str(config["version"])

        # Heuristics based on config structure
        if "rebac" in config:
            return "0.6.0"

        if "zone_id" in config:
            return "0.5.0"

        # Default to oldest known format
        return "0.4.0"

    def _apply_general_migrations(
        self,
        config: dict[str, Any],
        from_version: str,
        to_version: str,
        result: ConfigMigrationResult,
    ) -> dict[str, Any]:
        """Apply general migration transformations.

        Args:
            config: Configuration to migrate
            from_version: Source version
            to_version: Target version
            result: Result object to update

        Returns:
            Migrated configuration
        """
        # Remove deprecated fields
        deprecated_fields = self._get_deprecated_fields(from_version, to_version)
        for field_name in deprecated_fields:
            if field_name in config:
                del config[field_name]
                result.changes_made.append(f"Removed deprecated field: {field_name}")
                result.warnings.append(f"Field '{field_name}' is deprecated in {to_version}")

        # Apply field renames
        renames = self._get_field_renames(from_version, to_version)
        for old_name, new_name in renames.items():
            if old_name in config:
                config[new_name] = config.pop(old_name)
                result.changes_made.append(f"Renamed field: {old_name} -> {new_name}")

        # Add new required fields with defaults
        new_fields = self._get_new_required_fields(from_version, to_version)
        for field_name, default_value in new_fields.items():
            if field_name not in config:
                config[field_name] = default_value
                result.changes_made.append(f"Added new field: {field_name} = {default_value}")

        return config

    def _get_deprecated_fields(self, from_version: str, to_version: str) -> list[str]:
        """Get list of fields deprecated between versions.

        Args:
            from_version: Source version
            to_version: Target version

        Returns:
            List of deprecated field names
        """
        deprecated: list[str] = []

        # Add version-specific deprecations
        if from_version < "0.6.0" <= to_version:
            # Unix permissions removed in v0.6.0
            deprecated.extend(["default_mode", "umask"])

        return deprecated

    def _get_field_renames(
        self,
        from_version: str,  # noqa: ARG002 - Reserved for version-specific renames
        to_version: str,  # noqa: ARG002 - Reserved for version-specific renames
    ) -> dict[str, str]:
        """Get field renames between versions.

        Args:
            from_version: Source version
            to_version: Target version

        Returns:
            Dictionary mapping old names to new names
        """
        renames: dict[str, str] = {}

        # Add version-specific renames
        # if from_version < "0.7.0" <= to_version:
        #     renames["old_field"] = "new_field"

        return renames

    def _get_new_required_fields(
        self,
        from_version: str,  # noqa: ARG002 - Reserved for version-specific fields
        to_version: str,  # noqa: ARG002 - Reserved for version-specific fields
    ) -> dict[str, Any]:
        """Get new required fields with defaults.

        Args:
            from_version: Source version
            to_version: Target version

        Returns:
            Dictionary mapping field names to default values
        """
        new_fields: dict[str, Any] = {}

        # Add version-specific new fields
        # if from_version < "0.7.0" <= to_version:
        #     new_fields["new_required_field"] = "default_value"

        return new_fields
