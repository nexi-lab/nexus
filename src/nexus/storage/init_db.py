"""Simple database schema initialization.

This module provides a clean way to initialize the database schema
without instantiating backends or triggering any business logic.
"""

import contextlib
import sys

from sqlalchemy import create_engine, inspect, text

from nexus.storage.models import Base


def create_views(engine):
    """Create SQL views for work detection.

    Compatible with both SQLite and PostgreSQL.
    """
    with engine.begin() as conn, contextlib.suppress(Exception):
        # Drop existing view if it exists (PostgreSQL needs OR REPLACE, SQLite uses IF NOT EXISTS)
        # Use IF NOT EXISTS which works on both
        # View might already exist (PostgreSQL doesn't support IF NOT EXISTS in older versions)
        conn.execute(
            text("""
            CREATE VIEW IF NOT EXISTS pending_work AS
            SELECT
                path_id,
                virtual_path,
                backend_id,
                physical_path,
                updated_at
            FROM file_paths
            WHERE deleted_at IS NULL
            ORDER BY updated_at DESC
        """)
        )


def init_database(database_url: str) -> None:
    """Initialize database schema.

    Args:
        database_url: SQLAlchemy database URL

    Raises:
        Exception: If database initialization fails
    """
    # Create engine
    engine = create_engine(database_url)

    # Check if schema already exists
    # Use file_paths as the canary table (first core table in models.py)
    inspector = inspect(engine)
    tables = inspector.get_table_names()

    if "file_paths" in tables:
        print("✓ Database schema already exists")
        return

    # Create all tables from models
    print("Creating database schema...")
    Base.metadata.create_all(engine)

    # Create SQL views
    create_views(engine)

    print("✓ Database schema created successfully")


def main():
    """CLI entry point for database initialization."""
    if len(sys.argv) < 2:
        print("Usage: python -m nexus.storage.init_db <database_url>", file=sys.stderr)
        sys.exit(1)

    database_url = sys.argv[1]

    try:
        init_database(database_url)
    except Exception as e:
        print(f"ERROR: Failed to initialize database: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
