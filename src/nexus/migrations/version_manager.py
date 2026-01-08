"""Version manager for orchestrating upgrades and rollbacks.

This module provides the core logic for:
- Detecting current Nexus version
- Creating backups before migrations
- Executing migration steps
- Recording migration history
- Rolling back failed migrations
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import nexus
from nexus.migrations.registry import MigrationPath, get_registry

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from nexus.config import NexusConfig


@dataclass
class MigrationContext:
    """Context passed to migration functions.

    Attributes:
        config: Nexus configuration
        session: Database session for migrations
        dry_run: If True, simulate without making changes
        backup_path: Path where backup was created (if any)
        progress_callback: Optional callback for progress updates
    """

    config: NexusConfig
    session: Session | None = None
    dry_run: bool = False
    backup_path: str | None = None
    progress_callback: Callable[[str, int, int], None] | None = None

    def report_progress(self, message: str, current: int, total: int) -> None:
        """Report migration progress.

        Args:
            message: Status message
            current: Current step number
            total: Total number of steps
        """
        if self.progress_callback:
            self.progress_callback(message, current, total)


@dataclass
class MigrationResult:
    """Result of a migration operation.

    Attributes:
        success: Whether the migration completed successfully
        from_version: Starting version
        to_version: Target version
        steps_completed: Number of steps successfully completed
        steps_total: Total number of steps in the migration
        errors: List of error messages
        warnings: List of warning messages
        backup_path: Path to backup if one was created
        duration_seconds: Total time taken for migration
    """

    success: bool
    from_version: str
    to_version: str
    steps_completed: int = 0
    steps_total: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    backup_path: str | None = None
    duration_seconds: float = 0.0

    def __str__(self) -> str:
        """Human-readable summary."""
        status = "SUCCESS" if self.success else "FAILED"
        return (
            f"MigrationResult({status}: {self.from_version} -> {self.to_version}, "
            f"steps={self.steps_completed}/{self.steps_total}, "
            f"errors={len(self.errors)}, warnings={len(self.warnings)}, "
            f"duration={self.duration_seconds:.2f}s)"
        )


@dataclass
class MigrationHistoryEntry:
    """Record of a migration execution.

    Attributes:
        id: Unique identifier
        from_version: Source version
        to_version: Target version
        migration_type: Type of migration (upgrade, rollback, import)
        status: Current status (pending, running, completed, failed, rolled_back)
        backup_path: Path to backup if created
        started_at: When migration started
        completed_at: When migration completed (if finished)
        error_message: Error message if failed
        metadata: Additional JSON metadata
    """

    id: str
    from_version: str
    to_version: str
    migration_type: str
    status: str
    backup_path: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    metadata: dict | None = None


class VersionManager:
    """Orchestrates version upgrades and rollbacks.

    The VersionManager handles:
    - Version detection
    - Backup creation and restoration
    - Migration execution
    - History recording
    - Rollback support

    Example:
        manager = VersionManager(config)
        result = manager.upgrade("0.5.0", "0.6.0", backup=True)
        if not result.success:
            manager.rollback(result.from_version)
    """

    def __init__(self, config: NexusConfig) -> None:
        """Initialize version manager.

        Args:
            config: Nexus configuration
        """
        self.config = config
        self.registry = get_registry()
        self._session: Session | None = None

    def get_current_version(self) -> str:
        """Get the currently installed Nexus version.

        Returns:
            Version string (e.g., "0.6.4")
        """
        return nexus.__version__

    def get_target_version(self) -> str:
        """Get the target version (latest available).

        Returns:
            Latest version string from registry, or current if no migrations
        """
        versions = self.registry.get_available_versions()
        return versions[-1] if versions else self.get_current_version()

    def get_migration_history(self) -> list[MigrationHistoryEntry]:
        """Get migration history from database.

        Returns:
            List of migration history entries, newest first
        """
        # Import here to avoid circular imports
        from nexus.storage.metadata_store import SQLAlchemyMetadataStore
        from nexus.storage.models import MigrationHistoryModel

        store = SQLAlchemyMetadataStore(db_path=self.config.db_path)

        try:
            with store.SessionLocal() as session:
                records = (
                    session.query(MigrationHistoryModel)
                    .order_by(MigrationHistoryModel.started_at.desc())
                    .all()
                )

                return [
                    MigrationHistoryEntry(
                        id=r.id,
                        from_version=r.from_version,
                        to_version=r.to_version,
                        migration_type=r.migration_type,
                        status=r.status,
                        backup_path=r.backup_path,
                        started_at=r.started_at,
                        completed_at=r.completed_at,
                        error_message=r.error_message,
                        metadata=json.loads(r.metadata_json) if r.metadata_json else None,
                    )
                    for r in records
                ]
        finally:
            store.close()

    def plan_upgrade(self, from_version: str, to_version: str) -> MigrationPath | None:
        """Plan an upgrade path between versions.

        Args:
            from_version: Starting version
            to_version: Target version

        Returns:
            MigrationPath if valid path exists, None otherwise
        """
        return self.registry.get_migration_path(from_version, to_version)

    def upgrade(
        self,
        from_version: str,
        to_version: str,
        backup: bool = True,
        dry_run: bool = False,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> MigrationResult:
        """Execute upgrade from one version to another.

        Args:
            from_version: Starting version
            to_version: Target version
            backup: Whether to create backup before migration
            dry_run: If True, simulate without making changes
            progress_callback: Optional callback for progress updates

        Returns:
            MigrationResult with details of the migration
        """
        start_time = time.time()

        # Find migration path
        path = self.plan_upgrade(from_version, to_version)
        if path is None:
            return MigrationResult(
                success=False,
                from_version=from_version,
                to_version=to_version,
                errors=[f"No migration path found from {from_version} to {to_version}"],
            )

        result = MigrationResult(
            success=False,
            from_version=from_version,
            to_version=to_version,
            steps_total=len(path.steps),
        )

        # Check if backup is required
        if path.total_requires_backup and not backup and not dry_run:
            result.warnings.append(
                "Migration requires backup but --backup was not specified. "
                "Proceeding without backup."
            )

        # Create backup if requested
        backup_path = None
        if backup and not dry_run:
            try:
                backup_path = self.create_backup()
                result.backup_path = backup_path
            except Exception as e:
                result.errors.append(f"Failed to create backup: {e}")
                return result

        # Record migration start
        history_id = None
        if not dry_run:
            history_id = self._record_migration_start(
                from_version, to_version, "upgrade", backup_path
            )

        # Create migration context
        context = MigrationContext(
            config=self.config,
            dry_run=dry_run,
            backup_path=backup_path,
            progress_callback=progress_callback,
        )

        # Execute migration steps
        try:
            for i, step in enumerate(path.steps):
                context.report_progress(f"Executing: {step.name}", i + 1, len(path.steps))

                if step.is_destructive:
                    result.warnings.append(
                        f"Step '{step.name}' is destructive and may cause data loss"
                    )

                step_result = step.migrate_fn(context)

                if not step_result.success:
                    result.errors.extend(step_result.errors)
                    result.warnings.extend(step_result.warnings)
                    break

                result.steps_completed += 1
                result.warnings.extend(step_result.warnings)

            result.success = result.steps_completed == result.steps_total

        except Exception as e:
            result.errors.append(f"Migration failed with exception: {e}")

        # Record migration completion
        result.duration_seconds = time.time() - start_time
        if not dry_run and history_id:
            self._record_migration_complete(
                history_id,
                "completed" if result.success else "failed",
                result.errors[0] if result.errors else None,
            )

        return result

    def rollback(
        self,
        to_version: str,
        from_backup: str | None = None,
        dry_run: bool = False,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> MigrationResult:
        """Rollback to a previous version.

        Args:
            to_version: Target version to rollback to
            from_backup: Optional path to backup to restore
            dry_run: If True, simulate without making changes
            progress_callback: Optional callback for progress updates

        Returns:
            MigrationResult with details of the rollback
        """
        start_time = time.time()
        current_version = self.get_current_version()

        result = MigrationResult(
            success=False,
            from_version=current_version,
            to_version=to_version,
        )

        # If backup path provided, restore from backup
        if from_backup:
            if dry_run:
                result.warnings.append(f"Would restore from backup: {from_backup}")
                result.success = True
            else:
                try:
                    self.restore_backup(from_backup)
                    result.success = True
                except Exception as e:
                    result.errors.append(f"Failed to restore backup: {e}")

            result.duration_seconds = time.time() - start_time
            return result

        # Otherwise, find rollback path using reverse migrations
        path = self._find_rollback_path(current_version, to_version)
        if path is None:
            result.errors.append(
                f"No rollback path found from {current_version} to {to_version}. "
                "Consider restoring from a backup instead."
            )
            return result

        result.steps_total = len(path.steps)

        # Record rollback start
        history_id = None
        if not dry_run:
            history_id = self._record_migration_start(current_version, to_version, "rollback", None)

        # Create context
        context = MigrationContext(
            config=self.config,
            dry_run=dry_run,
            progress_callback=progress_callback,
        )

        # Execute rollback steps
        try:
            for i, step in enumerate(path.steps):
                if step.rollback_fn is None:
                    result.errors.append(f"Step '{step.name}' has no rollback function")
                    break

                context.report_progress(f"Rolling back: {step.name}", i + 1, len(path.steps))

                step_result = step.rollback_fn(context)

                if not step_result.success:
                    result.errors.extend(step_result.errors)
                    break

                result.steps_completed += 1

            result.success = result.steps_completed == result.steps_total

        except Exception as e:
            result.errors.append(f"Rollback failed with exception: {e}")

        # Record completion
        result.duration_seconds = time.time() - start_time
        if not dry_run and history_id:
            self._record_migration_complete(
                history_id,
                "completed" if result.success else "failed",
                result.errors[0] if result.errors else None,
            )

        return result

    def create_backup(self) -> str:
        """Create a backup of the current database and configuration.

        Returns:
            Path to the backup directory

        Raises:
            IOError: If backup creation fails
            ValueError: If data_dir is not configured
        """
        data_dir = self.config.data_dir
        if not data_dir:
            raise ValueError("data_dir must be configured for backup operations")

        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        backup_dir = Path(data_dir) / "backups" / f"backup_{timestamp}"
        backup_dir.mkdir(parents=True, exist_ok=True)

        # Backup database
        db_path = self.config.db_path
        if db_path and Path(db_path).exists():
            shutil.copy2(db_path, backup_dir / "nexus.db")

        # Backup config if exists
        config_path = Path(data_dir) / "nexus.yaml"
        if config_path.exists():
            shutil.copy2(config_path, backup_dir / "nexus.yaml")

        # Save version info
        version_info = {
            "nexus_version": nexus.__version__,
            "backup_time": datetime.now(UTC).isoformat(),
            "data_dir": data_dir,
        }
        (backup_dir / "version.json").write_text(json.dumps(version_info, indent=2))

        return str(backup_dir)

    def restore_backup(self, backup_path: str) -> bool:
        """Restore from a backup.

        Args:
            backup_path: Path to backup directory

        Returns:
            True if restoration succeeded

        Raises:
            FileNotFoundError: If backup path doesn't exist
            IOError: If restoration fails
            ValueError: If data_dir is not configured
        """
        data_dir = self.config.data_dir
        if not data_dir:
            raise ValueError("data_dir must be configured for restore operations")

        backup_dir = Path(backup_path)
        if not backup_dir.exists():
            raise FileNotFoundError(f"Backup not found: {backup_path}")

        # Restore database
        backup_db = backup_dir / "nexus.db"
        db_path = self.config.db_path
        if backup_db.exists() and db_path:
            shutil.copy2(backup_db, db_path)

        # Restore config
        backup_config = backup_dir / "nexus.yaml"
        config_path = Path(data_dir) / "nexus.yaml"
        if backup_config.exists():
            shutil.copy2(backup_config, config_path)

        return True

    def list_backups(self) -> list[dict]:
        """List available backups.

        Returns:
            List of backup info dictionaries
        """
        data_dir = self.config.data_dir
        if not data_dir:
            return []

        backups_dir = Path(data_dir) / "backups"
        if not backups_dir.exists():
            return []

        backups = []
        for backup_dir in sorted(backups_dir.iterdir(), reverse=True):
            if backup_dir.is_dir():
                version_file = backup_dir / "version.json"
                if version_file.exists():
                    info = json.loads(version_file.read_text())
                    info["path"] = str(backup_dir)
                    backups.append(info)
                else:
                    backups.append(
                        {
                            "path": str(backup_dir),
                            "nexus_version": "unknown",
                            "backup_time": None,
                        }
                    )

        return backups

    def _find_rollback_path(self, from_version: str, to_version: str) -> MigrationPath | None:
        """Find rollback path by reversing upgrade steps.

        Args:
            from_version: Current version
            to_version: Target version to rollback to

        Returns:
            MigrationPath with rollback steps, or None if not possible
        """
        # Get forward path and reverse it
        forward_path = self.registry.get_migration_path(to_version, from_version)
        if forward_path is None:
            return None

        # Check all steps have rollback functions
        if not forward_path.all_rollbackable:
            return None

        # Reverse the steps
        return MigrationPath(
            from_version=from_version,
            to_version=to_version,
            steps=list(reversed(forward_path.steps)),
        )

    def _record_migration_start(
        self,
        from_version: str,
        to_version: str,
        migration_type: str,
        backup_path: str | None,
    ) -> str:
        """Record migration start in history table.

        Args:
            from_version: Source version
            to_version: Target version
            migration_type: Type of migration
            backup_path: Path to backup if created

        Returns:
            ID of the history record
        """
        import uuid

        from nexus.storage.metadata_store import SQLAlchemyMetadataStore
        from nexus.storage.models import MigrationHistoryModel

        store = SQLAlchemyMetadataStore(db_path=self.config.db_path)

        try:
            with store.SessionLocal() as session:
                record = MigrationHistoryModel(
                    id=str(uuid.uuid4()),
                    from_version=from_version,
                    to_version=to_version,
                    migration_type=migration_type,
                    status="running",
                    backup_path=backup_path,
                    started_at=datetime.now(UTC),
                )
                session.add(record)
                session.commit()
                return record.id
        finally:
            store.close()

    def _record_migration_complete(
        self, history_id: str, status: str, error_message: str | None
    ) -> None:
        """Update migration history record on completion.

        Args:
            history_id: ID of the history record
            status: Final status
            error_message: Error message if failed
        """
        from nexus.storage.metadata_store import SQLAlchemyMetadataStore
        from nexus.storage.models import MigrationHistoryModel

        store = SQLAlchemyMetadataStore(db_path=self.config.db_path)

        try:
            with store.SessionLocal() as session:
                record = session.query(MigrationHistoryModel).filter_by(id=history_id).first()
                if record:
                    record.status = status
                    record.completed_at = datetime.now(UTC)
                    record.error_message = error_message
                    session.commit()
        finally:
            store.close()
