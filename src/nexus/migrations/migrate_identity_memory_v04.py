"""Migration script for Identity-Based Memory System.

Migrates existing Nexus installations to support:
- Entity Registry table
- Memory table with identity relationships
- Backward compatibility for existing data
"""

import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Table, inspect, text
from sqlalchemy.orm import Session

from nexus.storage.models import EntityRegistryModel, MemoryModel

logger = logging.getLogger(__name__)


class IdentityMemoryMigration:
    """Migration handler for identity-based memory system."""

    def __init__(self, session: Session):
        """Initialize migration.

        Args:
            session: SQLAlchemy database session.
        """
        self.session = session
        if session.bind is None:
            raise ValueError("Session must have a bind")
        self.inspector = inspect(session.bind)

    def needs_migration(self) -> bool:
        """Check if migration is needed.

        Returns:
            True if migration tables don't exist.
        """
        tables = self.inspector.get_table_names()
        return "entity_registry" not in tables or "memories" not in tables

    def create_tables(self) -> None:
        """Create new tables for identity-based memory system."""
        from nexus.storage.models import Base

        # Create tables if they don't exist
        bind = self.session.bind
        if bind is None:
            raise ValueError("Session must have a bind")

        tables: list[Table] = [
            EntityRegistryModel.__table__,  # type: ignore[list-item]
            MemoryModel.__table__,  # type: ignore[list-item]
        ]
        Base.metadata.create_all(bind, tables=tables)

        logger.info("Created entity_registry and memories tables")

    def migrate_file_metadata_to_memories(self) -> int:
        """Migrate existing file metadata to memories table.

        For backward compatibility, extracts memory-like files from
        file_paths table and migrates them to memories table.

        Returns:
            Number of records migrated.
        """
        # Check if we have memory-related paths in file_paths
        try:
            result = self.session.execute(
                text("""
                    SELECT path_id, tenant_id, virtual_path, content_hash, owner, "group", mode, created_at
                    FROM file_paths
                    WHERE virtual_path LIKE '%/memory/%' OR virtual_path LIKE '%/.nexus/memory/%'
                """)
            )

            migrated = 0
            for row in result:
                # Extract IDs from path (best effort)
                path_id, tenant_id, virtual_path, content_hash, owner, group, mode, created_at = row

                # Parse agent_id from path
                parts = virtual_path.split("/")
                agent_id = None
                user_id = None

                # Try to extract agent_id from typical workspace path
                if "workspace" in parts:
                    idx = parts.index("workspace")
                    if len(parts) > idx + 2:
                        # Assume /workspace/{tenant}/{agent}/ pattern
                        agent_id = parts[idx + 2]
                        # For backward compatibility, use agent_id as user_id initially
                        user_id = owner or agent_id

                # Skip if no content hash (not a real file)
                if not content_hash:
                    continue

                # Create memory entry
                try:
                    memory = MemoryModel(
                        content_hash=content_hash,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        agent_id=agent_id,
                        scope="agent",  # Default to agent scope
                        visibility="private",  # Default to private
                        group=group,
                        mode=mode or 420,  # 0o644
                        created_at=created_at or datetime.now(UTC),
                    )

                    self.session.add(memory)
                    migrated += 1

                except Exception as e:
                    logger.warning(f"Failed to migrate path {virtual_path}: {e}")
                    continue

            if migrated > 0:
                self.session.commit()
                logger.info(f"Migrated {migrated} file entries to memories table")

            return migrated

        except Exception as e:
            logger.warning(f"Could not migrate file metadata to memories: {e}")
            self.session.rollback()
            return 0

    def register_entities_from_config(
        self, tenant_id: str | None = None, user_id: str | None = None, agent_id: str | None = None
    ) -> None:
        """Register entities from configuration.

        Args:
            tenant_id: Tenant ID to register.
            user_id: User ID to register.
            agent_id: Agent ID to register.
        """
        from nexus.core.entity_registry import EntityRegistry

        registry = EntityRegistry(self.session)

        if tenant_id:
            registry.register_entity("tenant", tenant_id)
            logger.info(f"Registered tenant: {tenant_id}")

        if user_id:
            registry.register_entity(
                "user", user_id, parent_type="tenant" if tenant_id else None, parent_id=tenant_id
            )
            logger.info(f"Registered user: {user_id}")

        if agent_id:
            registry.register_entity(
                "agent", agent_id, parent_type="user" if user_id else None, parent_id=user_id
            )
            logger.info(f"Registered agent: {agent_id}")

    def extract_entities_from_file_paths(self) -> None:
        """Extract and register entities from existing file paths.

        Analyzes existing file_paths to discover entities and register them.
        """
        from nexus.core.entity_registry import EntityRegistry

        registry = EntityRegistry(self.session)

        try:
            # Extract tenant IDs
            result = self.session.execute(
                text("SELECT DISTINCT tenant_id FROM file_paths WHERE tenant_id IS NOT NULL")
            )
            for (tenant_id,) in result:
                registry.register_entity("tenant", tenant_id)

            # Extract agent IDs from workspace paths
            result = self.session.execute(
                text("""
                    SELECT DISTINCT virtual_path, tenant_id
                    FROM file_paths
                    WHERE virtual_path LIKE '/workspace/%'
                """)
            )

            for virtual_path, _tenant_id in result:
                parts = virtual_path.split("/")
                if len(parts) >= 4 and parts[1] == "workspace":
                    # /workspace/{tenant}/{agent}/...
                    # parts[2] might be agent_id
                    potential_agent = parts[3] if len(parts) > 3 else None

                    if potential_agent:
                        # Register as agent (we don't have user info, so no parent)
                        with contextlib.suppress(Exception):
                            registry.register_entity("agent", potential_agent)

            logger.info("Extracted and registered entities from file paths")

        except Exception as e:
            logger.warning(f"Could not extract entities from file paths: {e}")

    def run_migration(
        self,
        tenant_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        migrate_files: bool = False,
    ) -> dict[str, Any]:
        """Run complete migration.

        Args:
            tenant_id: Optional tenant ID to register.
            user_id: Optional user ID to register.
            agent_id: Optional agent ID to register.
            migrate_files: Whether to migrate existing files to memories.

        Returns:
            Dictionary with migration results.
        """
        results: dict[str, Any] = {
            "tables_created": False,
            "entities_registered": 0,
            "files_migrated": 0,
            "errors": [],
        }

        try:
            # Step 1: Create tables
            if self.needs_migration():
                self.create_tables()
                results["tables_created"] = True

            # Step 2: Register entities from config
            if tenant_id or user_id or agent_id:
                self.register_entities_from_config(tenant_id, user_id, agent_id)
                results["entities_registered"] += 1

            # Step 3: Extract entities from existing data
            try:
                self.extract_entities_from_file_paths()
            except Exception as e:
                results["errors"].append(f"Entity extraction failed: {e}")

            # Step 4: Migrate files (optional)
            if migrate_files:
                try:
                    results["files_migrated"] = self.migrate_file_metadata_to_memories()
                except Exception as e:
                    results["errors"].append(f"File migration failed: {e}")

        except Exception as e:
            logger.error(f"Migration failed: {e}")
            results["errors"].append(str(e))

        return results


def run_migration_from_config(session: Session, config: dict) -> dict:
    """Convenience function to run migration from Nexus config.

    Args:
        session: Database session.
        config: Nexus configuration dictionary.

    Returns:
        Migration results.
    """
    migration = IdentityMemoryMigration(session)

    return migration.run_migration(
        tenant_id=config.get("tenant_id"),
        user_id=config.get("user_id"),
        agent_id=config.get("agent_id"),
        migrate_files=config.get("migrate_files", False),
    )


if __name__ == "__main__":
    """Run migration as standalone script."""
    import sys

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 2:
        print("Usage: python migrate_identity_memory_v04.py <database_url>")
        print("Example: python migrate_identity_memory_v04.py sqlite:///./nexus-data/nexus.db")
        sys.exit(1)

    database_url = sys.argv[1]

    # Create engine and session
    engine = create_engine(database_url)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    try:
        migration = IdentityMemoryMigration(session)

        print("Checking if migration is needed...")
        if not migration.needs_migration():
            print("Migration already complete or not needed.")
            sys.exit(0)

        print("Running migration...")
        results = migration.run_migration(migrate_files=True)

        print("\nMigration Results:")
        print(f"  Tables created: {results['tables_created']}")
        print(f"  Entities registered: {results['entities_registered']}")
        print(f"  Files migrated: {results['files_migrated']}")

        if results["errors"]:
            print("\nErrors:")
            for error in results["errors"]:
                print(f"  - {error}")

        print("\nMigration complete!")

    finally:
        session.close()
