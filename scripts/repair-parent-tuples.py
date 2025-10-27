#!/usr/bin/env python3
"""Repair missing parent tuples for existing directories.

This script finds directories that don't have parent relationship tuples
and creates them. This is useful after upgrading from a version that didn't
create parent tuples automatically.

Usage:
    python scripts/repair-parent-tuples.py [--dry-run] [--path PATH]

Options:
    --dry-run    Show what would be done without making changes
    --path PATH  Only repair tuples under this path (default: /)
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nexus.cli.utils import BackendConfig, get_filesystem

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def find_directories_without_parent_tuples(nx: Any, base_path: str = "/") -> list[str]:
    """Find all directories that don't have parent tuples."""
    from sqlalchemy import text

    if not hasattr(nx, "_rebac_manager"):
        logger.error("ReBAC is not available in this NexusFS instance")
        return []

    with nx._rebac_manager.session_factory() as session:
        # Find all directory paths
        result = session.execute(
            text(
                """
            SELECT DISTINCT fp.virtual_path
            FROM file_paths fp
            LEFT JOIN rebac_tuples rt ON rt.object_id = fp.virtual_path
                                      AND rt.relation = 'parent'
                                      AND rt.object_type = 'file'
            WHERE fp.virtual_path LIKE :base_path || '%'
              AND fp.virtual_path != :base_path
              AND rt.tuple_id IS NULL
            ORDER BY fp.virtual_path
            """
            ),
            {"base_path": base_path},
        )

        paths = [row[0] for row in result]

    # Filter to only include directories (paths that have children or explicit metadata as directory)
    directories = []
    for path in paths:
        try:
            # Check if it's a directory by seeing if metadata exists and is a directory
            metadata = nx.metadata.get(path)
            if metadata and metadata.content_hash is None:  # Directories have no content_hash
                directories.append(path)
        except Exception as e:
            logger.debug(f"Skipping {path}: {e}")
            continue

    return directories


def repair_directory(nx: Any, path: str, dry_run: bool = False) -> bool:
    """Create parent tuple for a directory."""
    if not hasattr(nx, "_hierarchy_manager"):
        logger.error("Hierarchy manager not available")
        return False

    try:
        if dry_run:
            logger.info(f"[DRY RUN] Would create parent tuple for: {path}")
            return True

        # Get tenant_id from default context
        tenant_id = nx._default_context.tenant_id if hasattr(nx, "_default_context") else None

        created_count = nx._hierarchy_manager.ensure_parent_tuples(path, tenant_id=tenant_id)

        if created_count > 0:
            logger.info(f"✓ Created {created_count} parent tuple(s) for: {path}")
            return True
        else:
            logger.debug(f"  No parent tuples needed for: {path}")
            return False

    except Exception as e:
        logger.error(f"✗ Failed to create parent tuple for {path}: {e}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair missing parent tuples for directories")
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be done without making changes"
    )
    parser.add_argument(
        "--path", default="/", help="Only repair tuples under this path (default: /)"
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Get filesystem instance
    import os

    config = BackendConfig(
        backend=os.environ.get("NEXUS_BACKEND", "local"),
        data_dir=os.environ.get("NEXUS_DATA_DIR", "./nexus-data"),
        remote_url=os.environ.get("NEXUS_URL", ""),
        remote_api_key=os.environ.get("NEXUS_API_KEY", ""),
    )
    nx = get_filesystem(config)

    logger.info("=" * 60)
    logger.info("Repair Parent Tuples Utility")
    logger.info("=" * 60)
    logger.info(f"Mode: {'DRY RUN' if args.dry_run else 'REPAIR'}")
    logger.info(f"Base path: {args.path}")
    logger.info("")

    # Find directories without parent tuples
    logger.info("Scanning for directories without parent tuples...")
    directories = find_directories_without_parent_tuples(nx, args.path)

    if not directories:
        logger.info("✓ No directories need repair!")
        nx.close()
        return 0

    logger.info(f"Found {len(directories)} directories without parent tuples")
    logger.info("")

    # Repair each directory
    repaired = 0
    failed = 0

    for path in directories:
        if repair_directory(nx, path, dry_run=args.dry_run):
            repaired += 1
        else:
            failed += 1

    logger.info("")
    logger.info("=" * 60)
    logger.info("Summary:")
    logger.info(f"  Total directories: {len(directories)}")
    logger.info(f"  Repaired: {repaired}")
    logger.info(f"  Failed: {failed}")

    if args.dry_run:
        logger.info("")
        logger.info("This was a DRY RUN - no changes were made")
        logger.info("Run without --dry-run to apply changes")

    logger.info("=" * 60)

    nx.close()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
