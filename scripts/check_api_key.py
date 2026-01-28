#!/usr/bin/env python3
"""
Check if an API key exists in the database.

Usage:
    python scripts/check_api_key.py <database_url> <api_key>

Returns:
    EXISTS - if key is registered in database
    MISSING - if key is not found
"""

import sys
from pathlib import Path

# Add src to path for imports
script_dir = Path(__file__).parent
src_dir = script_dir.parent / "src"
sys.path.insert(0, str(src_dir))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from nexus.server.auth.database_key import DatabaseAPIKeyAuth  # noqa: E402


def check_api_key(database_url: str, api_key: str) -> str:
    """
    Check if API key exists in database.

    Args:
        database_url: Database connection URL
        api_key: API key to check

    Returns:
        "EXISTS" if key is found, "MISSING" otherwise
    """
    try:
        engine = create_engine(database_url)
        SessionFactory = sessionmaker(bind=engine)
        key_hash = DatabaseAPIKeyAuth._hash_key(api_key)

        with SessionFactory() as session:
            result = session.execute(
                text("SELECT 1 FROM api_keys WHERE key_hash = :hash"),
                {"hash": key_hash},
            ).fetchone()

            return "EXISTS" if result else "MISSING"
    except Exception:
        return "MISSING"


def main() -> None:
    """Main entry point."""
    if len(sys.argv) < 3:
        print("Usage: python check_api_key.py <database_url> <api_key>", file=sys.stderr)
        sys.exit(1)

    database_url = sys.argv[1]
    api_key = sys.argv[2]

    if not database_url or not api_key:
        print("MISSING")
        sys.exit(1)

    result = check_api_key(database_url, api_key)
    print(result)
    sys.exit(0 if result == "EXISTS" else 1)


if __name__ == "__main__":
    main()
