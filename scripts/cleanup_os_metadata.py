#!/usr/bin/env python3
"""Cleanup script for removing OS metadata files from Nexus.

This script scans the Nexus filesystem and removes OS-generated metadata files
like ._* (AppleDouble), .DS_Store, Thumbs.db, etc.

Usage:
    # Dry run (preview what would be deleted)
    python cleanup_os_metadata.py --dry-run

    # Delete OS metadata files
    python cleanup_os_metadata.py

    # Use remote server
    python cleanup_os_metadata.py --remote-url http://nexus.example.com:8080
"""

import argparse
import logging
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nexus import connect
from nexus.core.filters import is_os_metadata_file

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def scan_and_remove(
    nexus_fs: any,  # type: ignore[valid-type]
    path: str = "/",
    dry_run: bool = True,
    deleted_count: list[int] | None = None,
) -> None:
    """Recursively scan and remove OS metadata files.

    Args:
        nexus_fs: Nexus filesystem instance
        path: Path to start scanning from
        dry_run: If True, only preview what would be deleted
        deleted_count: Mutable list to track deletion count
    """
    if deleted_count is None:
        deleted_count = [0]

    try:
        # List files in current directory
        files = nexus_fs.list(path, recursive=False, details=False)  # type: ignore[attr-defined]

        for file_path in files:
            # Handle both string and dict formats
            if isinstance(file_path, dict):
                file_path = file_path.get("path", "")

            if not file_path:
                continue

            # Extract filename
            filename = file_path.split("/")[-1]

            # Check if it's an OS metadata file
            if is_os_metadata_file(filename):
                if dry_run:
                    logger.info(f"[DRY RUN] Would delete: {file_path}")
                else:
                    try:
                        nexus_fs.delete(file_path)  # type: ignore[attr-defined]
                        logger.info(f"Deleted: {file_path}")
                        deleted_count[0] += 1
                    except Exception as e:
                        logger.error(f"Error deleting {file_path}: {e}")
            # Recurse into directories
            elif nexus_fs.is_directory(file_path):  # type: ignore[attr-defined]
                scan_and_remove(nexus_fs, file_path, dry_run, deleted_count)

    except Exception as e:
        logger.error(f"Error scanning {path}: {e}")


def main() -> None:
    """Main entry point for cleanup script."""
    parser = argparse.ArgumentParser(description="Remove OS metadata files from Nexus filesystem")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be deleted without actually deleting",
    )
    parser.add_argument(
        "--remote-url",
        type=str,
        help="Remote Nexus RPC server URL (e.g., http://localhost:8080)",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        help="Local data directory (for local filesystem)",
    )
    parser.add_argument(
        "--backend",
        type=str,
        choices=["local", "gcs"],
        default="local",
        help="Backend type (default: local)",
    )
    parser.add_argument(
        "--gcs-bucket",
        type=str,
        help="GCS bucket name (required when backend=gcs)",
    )
    parser.add_argument(
        "--path",
        type=str,
        default="/",
        help="Starting path to scan (default: /)",
    )

    args = parser.parse_args()

    # Build connection config
    config: dict[str, str | bool] = {}
    if args.remote_url:
        config["remote_url"] = args.remote_url
    if args.data_dir:
        config["data_dir"] = args.data_dir
    if args.backend:
        config["backend"] = args.backend
    if args.gcs_bucket:
        config["gcs_bucket"] = args.gcs_bucket

    # Connect to Nexus
    try:
        logger.info("Connecting to Nexus...")
        nexus_fs = connect(config=config) if config else connect()
        logger.info("Connected successfully")
    except Exception as e:
        logger.error(f"Failed to connect to Nexus: {e}")
        sys.exit(1)

    # Run cleanup
    mode = "DRY RUN" if args.dry_run else "DELETE"
    logger.info(f"Starting cleanup scan ({mode})...")
    logger.info(f"Scanning from: {args.path}")

    deleted_count = [0]
    scan_and_remove(nexus_fs, args.path, args.dry_run, deleted_count)

    if args.dry_run:
        logger.info(f"Dry run completed. Would delete {deleted_count[0]} files.")
        logger.info("Run without --dry-run to actually delete files.")
    else:
        logger.info(f"Cleanup completed. Deleted {deleted_count[0]} files.")


if __name__ == "__main__":
    main()
