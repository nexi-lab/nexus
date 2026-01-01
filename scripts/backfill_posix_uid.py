#!/usr/bin/env python3
"""Backfill posix_uid from ReBAC direct_owner relationships.

Issue #920: POSIX Mode Bits for O(1) Permission Checks

This script populates the posix_uid column in file_paths table from
existing ReBAC direct_owner relationships. This enables the O(1) owner
fast-path for permission checks.

Usage:
    python scripts/backfill_posix_uid.py [--dry-run] [--batch-size N]

Options:
    --dry-run       Show what would be updated without making changes
    --batch-size N  Number of files to process per batch (default: 1000)
"""

import argparse
import os
import sys
from datetime import UTC, datetime

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def get_db_url() -> str:
    """Get database URL from environment."""
    return os.environ.get(
        "NEXUS_DATABASE_URL",
        os.environ.get("POSTGRES_URL", "sqlite:///nexus.db"),
    )


def backfill_posix_uid(dry_run: bool = False, batch_size: int = 1000) -> dict[str, int | str]:
    """Backfill posix_uid from ReBAC direct_owner relationships.

    Args:
        dry_run: If True, show what would be updated without making changes
        batch_size: Number of files to process per batch

    Returns:
        Dict with statistics about the backfill operation
    """
    from sqlalchemy import create_engine, text

    db_url = get_db_url()
    engine = create_engine(db_url)

    stats: dict[str, int | str] = {
        "total_files": 0,
        "files_without_owner": 0,
        "files_updated": 0,
        "files_skipped": 0,
        "errors": 0,
        "start_time": datetime.now(UTC).isoformat(),
    }

    print(f"Connecting to database: {db_url[:50]}...")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"Batch size: {batch_size}")
    print()

    with engine.connect() as conn:
        # Count total files without posix_uid
        count_query = text("""
            SELECT COUNT(*) FROM file_paths
            WHERE posix_uid IS NULL AND deleted_at IS NULL
        """)
        result = conn.execute(count_query)
        stats["files_without_owner"] = result.scalar() or 0

        print(f"Files without posix_uid: {stats['files_without_owner']}")

        if stats["files_without_owner"] == 0:
            print("Nothing to backfill!")
            return stats

        # For PostgreSQL, we can do this in a single UPDATE with JOIN
        if engine.dialect.name == "postgresql":
            if dry_run:
                # Show what would be updated
                preview_query = text("""
                    SELECT fp.virtual_path, rt.subject_id
                    FROM file_paths fp
                    JOIN rebac_tuples rt ON rt.object_id = fp.virtual_path
                    WHERE fp.posix_uid IS NULL
                      AND fp.deleted_at IS NULL
                      AND rt.relation = 'direct_owner'
                      AND rt.object_type = 'file'
                    LIMIT 20
                """)
                result = conn.execute(preview_query)
                print("\nPreview of updates (first 20):")
                for row in result:
                    print(f"  {row[0]} -> owner: {row[1]}")
            else:
                # Perform the actual update
                update_query = text("""
                    UPDATE file_paths fp
                    SET posix_uid = rt.subject_id
                    FROM rebac_tuples rt
                    WHERE rt.object_id = fp.virtual_path
                      AND fp.posix_uid IS NULL
                      AND fp.deleted_at IS NULL
                      AND rt.relation = 'direct_owner'
                      AND rt.object_type = 'file'
                """)
                result = conn.execute(update_query)
                stats["files_updated"] = result.rowcount
                conn.commit()
                print(f"Updated {stats['files_updated']} files")

        else:
            # SQLite: Need to do batch updates
            select_query = text("""
                SELECT fp.path_id, fp.virtual_path, rt.subject_id
                FROM file_paths fp
                JOIN rebac_tuples rt ON rt.object_id = fp.virtual_path
                WHERE fp.posix_uid IS NULL
                  AND fp.deleted_at IS NULL
                  AND rt.relation = 'direct_owner'
                  AND rt.object_type = 'file'
                LIMIT :batch_size
            """)

            while True:
                result = conn.execute(select_query, {"batch_size": batch_size})
                rows = result.fetchall()

                if not rows:
                    break

                if dry_run:
                    print(f"\nWould update {len(rows)} files:")
                    for row in rows[:10]:
                        print(f"  {row[1]} -> owner: {row[2]}")
                    if len(rows) > 10:
                        print(f"  ... and {len(rows) - 10} more")
                    stats["files_updated"] = int(stats["files_updated"]) + len(rows)
                    break  # Only show first batch in dry run
                else:
                    for row in rows:
                        path_id, virtual_path, owner_id = row
                        update_query = text("""
                            UPDATE file_paths
                            SET posix_uid = :owner_id
                            WHERE path_id = :path_id
                        """)
                        conn.execute(update_query, {"owner_id": owner_id, "path_id": path_id})
                        stats["files_updated"] = int(stats["files_updated"]) + 1

                    conn.commit()
                    print(f"Updated {stats['files_updated']} files so far...")

    stats["end_time"] = datetime.now(UTC).isoformat()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill posix_uid from ReBAC direct_owner relationships"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be updated without making changes",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Number of files to process per batch (default: 1000)",
    )
    args = parser.parse_args()

    stats = backfill_posix_uid(dry_run=args.dry_run, batch_size=args.batch_size)

    print("\n" + "=" * 50)
    print("Backfill Summary:")
    print(f"  Files without owner: {stats['files_without_owner']}")
    print(f"  Files updated: {stats['files_updated']}")
    print(f"  Start time: {stats['start_time']}")
    print(f"  End time: {stats.get('end_time', 'N/A')}")
    print("=" * 50)


if __name__ == "__main__":
    main()
