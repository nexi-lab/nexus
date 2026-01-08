"""Nexus Migration Tools - Version upgrades and system migrations.

This package provides infrastructure for:
- Version-to-version migrations
- Bulk data import from S3/GCS/local
- Integrity validation
- Rollback support
- Configuration migration

Issue #165: Migration Tools & Upgrade Paths
"""

from nexus.migrations.config_migrator import (
    ConfigMigrationResult,
    ConfigMigrator,
)
from nexus.migrations.data_migrator import (
    DataMigrator,
    FileInfo,
    ImportOptions,
    ImportResult,
)
from nexus.migrations.registry import (
    MigrationPath,
    MigrationRegistry,
    MigrationStep,
    get_registry,
)
from nexus.migrations.validators import (
    IntegrityValidator,
    ValidationResult,
)
from nexus.migrations.version_manager import (
    MigrationContext,
    MigrationHistoryEntry,
    MigrationResult,
    VersionManager,
)

__all__ = [
    # Registry
    "MigrationRegistry",
    "MigrationStep",
    "MigrationPath",
    "get_registry",
    # Version Manager
    "VersionManager",
    "MigrationContext",
    "MigrationResult",
    "MigrationHistoryEntry",
    # Data Migrator
    "DataMigrator",
    "ImportOptions",
    "ImportResult",
    "FileInfo",
    # Validators
    "IntegrityValidator",
    "ValidationResult",
    # Config Migrator
    "ConfigMigrator",
    "ConfigMigrationResult",
]
