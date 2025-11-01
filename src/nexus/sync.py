"""Nexus sync module - rclone-inspired file operations.

Provides efficient sync, copy, and move operations with hash-based
change detection and progress reporting.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus import NexusFilesystem


class SyncStats:
    """Statistics for sync operations."""

    def __init__(self) -> None:
        self.files_checked = 0
        self.files_copied = 0
        self.files_skipped = 0
        self.files_deleted = 0
        self.bytes_transferred = 0
        self.errors: list[str] = []


def is_local_path(path: str) -> bool:
    """Check if a path is a local filesystem path (not a Nexus path).

    Heuristic:
    - Relative paths (./data, data, ../data) are local
    - Absolute paths that exist on the filesystem are local
    - Paths starting with / that don't exist are assumed to be Nexus paths
    - Paths with parent directories that exist are local (except root /)
    """
    # Relative paths are always local
    if not path.startswith("/"):
        return True

    # Check if file exists
    if os.path.exists(path):
        return True

    # Check if parent directory exists (for new files)
    parent = os.path.dirname(path)
    # Exclude root directory / from the check - paths under / are likely Nexus paths
    # Return True if parent exists AND is not root, otherwise assume it's a Nexus path
    return bool(parent and parent != "/" and os.path.exists(parent))


def list_local_files(local_path: str, recursive: bool = True) -> list[str]:
    """List files in a local directory."""
    local_path_obj = Path(local_path)
    files = []

    if local_path_obj.is_file():
        return [str(local_path_obj)]

    if recursive:
        for item in local_path_obj.rglob("*"):
            if item.is_file():
                files.append(str(item))
    else:
        for item in local_path_obj.iterdir():
            if item.is_file():
                files.append(str(item))

    return files


def copy_file(
    nx: NexusFilesystem,
    source: str,
    dest: str,
    is_source_local: bool,
    is_dest_local: bool,
    checksum: bool = False,
) -> int:
    """Copy a single file between local and Nexus.

    Returns the number of bytes copied.
    """
    if is_source_local and is_dest_local:
        # Local to local - just use shutil
        import shutil

        shutil.copy2(source, dest)
        return os.path.getsize(source)

    elif is_source_local and not is_dest_local:
        # Local to Nexus
        with open(source, "rb") as f:
            content = f.read()

        # Check if destination exists and has same content (if checksum enabled)
        if checksum and nx.exists(dest):
            try:
                raw_existing = nx.read(dest)
                # Type narrowing: when return_metadata=False (default), result is bytes
                assert isinstance(raw_existing, bytes), "Expected bytes from read()"
                existing_content = raw_existing
                if existing_content == content:
                    return 0  # Skip - identical content
            except (OSError, FileNotFoundError, AssertionError):
                # Content is missing, corrupted, or wrong type - re-write it
                pass

        # Create parent directories in Nexus
        parent = str(PurePosixPath(dest).parent)
        if parent and parent != "/" and parent != ".":
            nx.mkdir(parent, parents=True, exist_ok=True)

        nx.write(dest, content)
        return len(content)

    elif not is_source_local and is_dest_local:
        # Nexus to local
        raw_content = nx.read(source)
        # Type narrowing: when return_metadata=False (default), result is bytes
        assert isinstance(raw_content, bytes), "Expected bytes from read()"
        content = raw_content

        # Check if destination exists and has same content (if checksum enabled)
        if checksum and os.path.exists(dest):
            with open(dest, "rb") as f:
                if f.read() == content:
                    return 0  # Skip - identical content

        # Create parent directories
        Path(dest).parent.mkdir(parents=True, exist_ok=True)

        with open(dest, "wb") as f:
            f.write(content)
        return len(content)

    else:
        # Nexus to Nexus
        raw_content = nx.read(source)
        # Type narrowing: when return_metadata=False (default), result is bytes
        assert isinstance(raw_content, bytes), "Expected bytes from read()"
        content = raw_content

        # Check if destination exists and has same content (if checksum enabled)
        if checksum and nx.exists(dest):
            try:
                raw_existing = nx.read(dest)
                # Type narrowing: when return_metadata=False (default), result is bytes
                assert isinstance(raw_existing, bytes), "Expected bytes from read()"
                existing_content = raw_existing
                if existing_content == content:
                    return 0  # Skip - identical content
            except (OSError, FileNotFoundError, AssertionError):
                # Content is missing, corrupted, or wrong type - re-write it
                pass

        # Create parent directories in Nexus
        parent = str(PurePosixPath(dest).parent)
        if parent and parent != "/" and parent != ".":
            nx.mkdir(parent, parents=True, exist_ok=True)

        nx.write(dest, content)
        return len(content)


def sync_directories(
    nx: NexusFilesystem,
    source: str,
    dest: str,
    delete: bool = False,
    dry_run: bool = False,
    checksum: bool = True,
    progress: bool = True,
) -> SyncStats:
    """Sync source directory to destination (one-way sync).

    Args:
        nx: Nexus filesystem instance
        source: Source path (local or Nexus)
        dest: Destination path (local or Nexus)
        delete: Delete files in dest that don't exist in source
        dry_run: Preview changes without making them
        checksum: Use hash-based comparison for change detection
        progress: Show progress bar (default: True)

    Returns:
        SyncStats with operation statistics
    """
    stats = SyncStats()

    is_source_local = is_local_path(source)
    is_dest_local = is_local_path(dest)

    # Get source files
    if is_source_local:
        source_files = list_local_files(source)
        # Convert to relative paths with forward slashes (POSIX-style)
        source_path = Path(source)
        source_files_rel = [Path(f).relative_to(source_path).as_posix() for f in source_files]
    else:
        source_files_abs = nx.list(source, recursive=True)
        # Ensure we have a list of strings (paths)
        if (
            isinstance(source_files_abs, list)
            and source_files_abs
            and isinstance(source_files_abs[0], dict)
        ):
            source_files_str: list[str] = [f["path"] for f in source_files_abs]  # type: ignore[index]
        else:
            source_files_str = source_files_abs  # type: ignore[assignment]
        # Convert to relative paths
        source_files_rel = [str(PurePosixPath(f).relative_to(source)) for f in source_files_str]

    # Get destination files (for delete operation)
    dest_files_rel = []
    if delete:
        if is_dest_local:
            dest_files = list_local_files(dest)
            dest_path = Path(dest)
            # Convert to relative paths with forward slashes (POSIX-style)
            dest_files_rel = [Path(f).relative_to(dest_path).as_posix() for f in dest_files]
        else:
            dest_files_abs = nx.list(dest, recursive=True)
            # Ensure we have a list of strings (paths)
            if (
                isinstance(dest_files_abs, list)
                and dest_files_abs
                and isinstance(dest_files_abs[0], dict)
            ):
                dest_files_str: list[str] = [f["path"] for f in dest_files_abs]  # type: ignore[index]
            else:
                dest_files_str = dest_files_abs  # type: ignore[assignment]
            dest_files_rel = [str(PurePosixPath(f).relative_to(dest)) for f in dest_files_str]

    # Copy files from source to dest with optional progress bar
    try:
        from tqdm import tqdm

        iterator = (
            tqdm(source_files_rel, desc="Syncing files", unit="file")
            if progress
            else source_files_rel
        )
    except ImportError:
        iterator = source_files_rel

    for rel_path in iterator:
        stats.files_checked += 1

        # Construct full paths
        if is_source_local:
            src_full = str(Path(source) / rel_path)
        else:
            src_full = str(PurePosixPath(source) / rel_path)

        if is_dest_local:
            dest_full = str(Path(dest) / rel_path)
        else:
            dest_full = str(PurePosixPath(dest) / rel_path)

        try:
            if not dry_run:
                bytes_copied = copy_file(
                    nx, src_full, dest_full, is_source_local, is_dest_local, checksum
                )
                if bytes_copied > 0:
                    stats.files_copied += 1
                    stats.bytes_transferred += bytes_copied
                else:
                    stats.files_skipped += 1
            else:
                # Dry run - just check if we would copy
                stats.files_copied += 1

        except Exception as e:
            stats.errors.append(f"{src_full}: {e}")

    # Delete files in dest that don't exist in source
    if delete:
        source_files_set = set(source_files_rel)

        try:
            from tqdm import tqdm

            delete_iterator = (
                tqdm(dest_files_rel, desc="Deleting extra files", unit="file")
                if progress
                else dest_files_rel
            )
        except ImportError:
            delete_iterator = dest_files_rel

        for rel_path in delete_iterator:
            if rel_path not in source_files_set:
                stats.files_deleted += 1

                if not dry_run:
                    if is_dest_local:
                        dest_full = str(Path(dest) / rel_path)
                        os.remove(dest_full)
                    else:
                        dest_full = str(PurePosixPath(dest) / rel_path)
                        nx.delete(dest_full)

    return stats


def copy_recursive(
    nx: NexusFilesystem,
    source: str,
    dest: str,
    checksum: bool = True,
    progress: bool = True,
) -> SyncStats:
    """Recursively copy files with optional checksum verification.

    This is similar to sync but without the delete option.
    """
    return sync_directories(nx, source, dest, delete=False, checksum=checksum, progress=progress)


def move_file(
    nx: NexusFilesystem,
    source: str,
    dest: str,
) -> bool:
    """Move a file or directory.

    Returns True if the operation was successful.
    """
    is_source_local = is_local_path(source)
    is_dest_local = is_local_path(dest)

    try:
        if is_source_local and is_dest_local:
            # Local to local - use os.rename or shutil.move
            import shutil

            shutil.move(source, dest)
            return True

        elif not is_source_local and not is_dest_local:
            # Nexus to Nexus - use efficient rename API
            # This is metadata-only, instant operation that preserves ReBAC permissions
            nx.rename(source, dest)
            return True

        else:
            # Cross-boundary move - copy then delete
            copy_file(nx, source, dest, is_source_local, is_dest_local)

            # Delete source
            if is_source_local:
                os.remove(source)
            else:
                nx.delete(source)
            return True

    except (OSError, ValueError, TypeError):
        # File operation or path validation failed
        return False
