#!/usr/bin/env python3
"""Database initialization script for Nexus.

Handles both fresh and existing databases:
- Fresh databases: Creates schema via SQLAlchemy, stamps with latest migration
- Existing databases: Runs pending migrations via Alembic

This replaces the old ORM auto-creation approach with proper migration-based setup.
"""

import os
import sys
from pathlib import Path

# Add src to path
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# Imports after path modification
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402

from alembic import command  # noqa: E402

# Path to alembic.ini (located in alembic/ directory)
ALEMBIC_INI_PATH = PROJECT_ROOT / "alembic" / "alembic.ini"


def init_database(database_url: str) -> None:
    """Initialize database schema and migrations.

    Args:
        database_url: SQLAlchemy database URL
    """
    print("🔍 Checking database state...")

    # Create engine
    engine = create_engine(database_url)
    inspector = inspect(engine)

    # Check if alembic_version table exists and has a version
    has_alembic_version = "alembic_version" in inspector.get_table_names()
    has_migration_version = False

    if has_alembic_version:
        # Check if table has any version recorded
        with engine.connect() as conn:
            result = conn.execute(text("SELECT version_num FROM alembic_version"))
            has_migration_version = result.fetchone() is not None

    # Check if base tables exist (check for a core table like 'file_paths')
    has_tables = "file_paths" in inspector.get_table_names()

    if has_migration_version:
        # Database has migration history - just run pending migrations
        print("✓ Database has migration history")
        print("🔄 Running pending migrations...")

        alembic_cfg = Config(str(ALEMBIC_INI_PATH))
        command.upgrade(alembic_cfg, "head")

        print("✓ Database migrations up to date")

    elif has_tables:
        # Database has tables but no migration history
        # This is an existing database created via ORM auto-creation
        print("⚠️  Database has tables but no migration history")
        print("📌 Stamping database with latest migration version...")

        alembic_cfg = Config(str(ALEMBIC_INI_PATH))
        command.stamp(alembic_cfg, "head")

        print("✓ Database stamped with current schema version")
        print("ℹ️  Future schema changes will be applied via migrations")

    else:
        # Fresh database - create schema from models
        print("📊 Fresh database detected - creating schema...")

        from nexus.storage.models import Base

        # Create all tables
        Base.metadata.create_all(engine)

        print("✓ Database schema created")
        print("📌 Stamping with latest migration version...")

        alembic_cfg = Config(str(ALEMBIC_INI_PATH))
        command.stamp(alembic_cfg, "head")

        print("✓ Database initialized successfully")

    engine.dispose()


def main() -> None:
    """Main entry point."""
    database_url = os.getenv("NEXUS_DATABASE_URL")
    if not database_url:
        print("ERROR: NEXUS_DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)

    try:
        init_database(database_url)
    except Exception as e:
        print(f"ERROR: Failed to initialize database: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
