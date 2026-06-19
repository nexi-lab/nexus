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

from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth  # noqa: E402
from nexus.core.db_utils import normalize_database_url  # noqa: E402


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
        # Issue #4238: accept the canonical ``postgres://`` scheme.
        database_url = normalize_database_url(database_url)
        engine = create_engine(database_url)
        SessionFactory = sessionmaker(bind=engine)
        key_hash = DatabaseAPIKeyAuth._hash_key(api_key)

        with SessionFactory() as session:
            row = session.execute(
                text(
                    """
                    SELECT key_id, zone_id, is_admin, revoked
                    FROM api_keys
                    WHERE key_hash = :hash
                    """
                ),
                {"hash": key_hash},
            ).fetchone()

            if not row or int(row.revoked or 0) != 0:
                return "MISSING"

            if row.zone_id is not None:
                matching_zone = session.execute(
                    text(
                        """
                        SELECT 1
                        FROM api_key_zones
                        WHERE key_id = :key_id AND zone_id = :zone_id
                        """
                    ),
                    {"key_id": row.key_id, "zone_id": row.zone_id},
                ).fetchone()
                if not matching_zone:
                    return "MISSING"

            if int(row.is_admin or 0) == 0:
                any_zone = session.execute(
                    text("SELECT 1 FROM api_key_zones WHERE key_id = :key_id"),
                    {"key_id": row.key_id},
                ).fetchone()
                if not any_zone:
                    return "MISSING"

            return "EXISTS"
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
