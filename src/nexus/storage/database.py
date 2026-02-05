"""Database connection and session management for Nexus.

This module provides the SQLAlchemy database connection used by various Nexus
components (auth, permissions, version history, etc.). File metadata uses
RaftMetadataStore (sled) instead.

Usage:
    from nexus.storage.database import get_session, get_engine

    # Get a session for database operations
    with get_session() as session:
        user = session.query(UserModel).filter_by(id=user_id).first()

    # Or use the session factory directly
    from nexus.storage.database import SessionLocal
    session = SessionLocal()
    try:
        ...
    finally:
        session.close()
"""

from __future__ import annotations

import logging
import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from nexus.storage.models import Base

logger = logging.getLogger(__name__)

# Global engine and session factory (lazy initialized)
_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def get_database_url(db_path: str | Path | None = None) -> str:
    """Get database URL from environment or parameter.

    Priority order:
    1. NEXUS_DATABASE_URL environment variable
    2. POSTGRES_URL environment variable
    3. db_path parameter (converted to SQLite URL)
    4. Default SQLite path: ./nexus-data/nexus.db

    Args:
        db_path: Optional path to SQLite database file

    Returns:
        Database URL string
    """
    url = (
        os.getenv("NEXUS_DATABASE_URL")
        or os.getenv("POSTGRES_URL")
        or (f"sqlite:///{db_path}" if db_path else None)
        or "sqlite:///./nexus-data/nexus.db"
    )
    return url


def get_engine(db_url: str | None = None, db_path: str | Path | None = None) -> Engine:
    """Get or create the SQLAlchemy engine.

    Args:
        db_url: Database URL (optional, uses env vars if not provided)
        db_path: Path to SQLite database (optional, fallback if no URL)

    Returns:
        SQLAlchemy Engine instance
    """
    global _engine

    if _engine is not None:
        return _engine

    database_url = db_url or get_database_url(db_path)
    db_type = "postgresql" if "postgresql" in database_url else "sqlite"

    logger.info(f"Initializing database: {db_type}")

    # Configure engine based on database type
    if db_type == "sqlite":
        # Extract path and ensure parent directory exists
        if database_url.startswith("sqlite:///"):
            path = Path(database_url[10:])
            path.parent.mkdir(parents=True, exist_ok=True)

        _engine = create_engine(
            database_url,
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
        )

        # Set SQLite busy timeout
        @event.listens_for(_engine, "connect")
        def set_sqlite_pragma(dbapi_conn: Any, _connection_record: Any) -> None:
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()
    else:
        # PostgreSQL configuration
        _engine = create_engine(
            database_url,
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
            pool_recycle=3600,
        )

    # Create tables if they don't exist
    Base.metadata.create_all(_engine)

    return _engine


def get_session_factory(
    db_url: str | None = None, db_path: str | Path | None = None
) -> sessionmaker:
    """Get or create the session factory.

    Args:
        db_url: Database URL (optional)
        db_path: Path to SQLite database (optional)

    Returns:
        SQLAlchemy sessionmaker instance
    """
    global _SessionLocal

    if _SessionLocal is not None:
        return _SessionLocal

    engine = get_engine(db_url, db_path)
    _SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    return _SessionLocal


def SessionLocal() -> Session:
    """Create a new database session.

    Returns:
        SQLAlchemy Session instance

    Note:
        Caller is responsible for closing the session.
        Prefer using get_session() context manager instead.
    """
    factory = get_session_factory()
    return factory()


@contextmanager
def get_session(
    db_url: str | None = None, db_path: str | Path | None = None
) -> Generator[Session, None, None]:
    """Context manager for database sessions.

    Automatically commits on success and rolls back on error.

    Args:
        db_url: Database URL (optional)
        db_path: Path to SQLite database (optional)

    Yields:
        SQLAlchemy Session instance

    Example:
        with get_session() as session:
            user = session.query(UserModel).filter_by(id=1).first()
    """
    factory = get_session_factory(db_url, db_path)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """Reset the global engine and session factory.

    Useful for testing or reconfiguration.
    """
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
