"""File filtering utilities for Nexus filesystem.

This module provides utilities for filtering out OS-generated metadata files
that should not be stored or displayed in Nexus.
"""

from fnmatch import fnmatch

# Try to import Rust acceleration
try:
    import nexus_fast

    RUST_AVAILABLE = True
except ImportError:
    RUST_AVAILABLE = False

# OS-generated metadata file patterns
# These files are automatically created by operating systems and should be
# filtered out to keep Nexus clean and efficient
OS_METADATA_PATTERNS = [
    "._*",  # AppleDouble files (macOS extended attributes)
    ".DS_Store",  # macOS Finder metadata
    "Thumbs.db",  # Windows thumbnail cache
    "desktop.ini",  # Windows folder customization
    ".Spotlight-V100",  # macOS Spotlight index
    ".Trashes",  # macOS trash folder
    ".fseventsd",  # macOS filesystem events daemon
    ".TemporaryItems",  # macOS temporary files
    ".VolumeIcon.icns",  # macOS custom folder icons
    ".com.apple.timemachine.donotpresent",  # macOS Time Machine
]


def is_os_metadata_file(path: str) -> bool:
    """Check if a file path represents OS-generated metadata.

    Args:
        path: File path or filename to check

    Returns:
        True if the path matches any OS metadata pattern, False otherwise

    Examples:
        >>> is_os_metadata_file("._test.txt")
        True
        >>> is_os_metadata_file(".DS_Store")
        True
        >>> is_os_metadata_file("my_file.txt")
        False
        >>> is_os_metadata_file("/path/to/._hidden")
        True
    """
    # Extract just the filename from the path
    filename = path.split("/")[-1] if "/" in path else path

    # Check if filename matches any OS metadata pattern
    return any(fnmatch(filename, pattern) for pattern in OS_METADATA_PATTERNS)


def filter_os_metadata(files: list[str]) -> list[str]:
    """Filter out OS metadata files from a list of file paths.

    Uses Rust acceleration if available (5-10x faster), otherwise falls back to Python.

    Args:
        files: List of file paths or filenames

    Returns:
        Filtered list with OS metadata files removed

    Examples:
        >>> filter_os_metadata(["file.txt", "._file.txt", ".DS_Store"])
        ['file.txt']
    """
    # Use Rust for bulk filtering if available (5-10x faster)
    if RUST_AVAILABLE and len(files) >= 10:
        try:
            return nexus_fast.filter_paths(files, OS_METADATA_PATTERNS)  # type: ignore[no-any-return]
        except Exception:
            # Fall back to Python on error
            pass

    # Python fallback
    return [f for f in files if not is_os_metadata_file(f)]


def filter_os_metadata_dicts(files: list[dict[str, any]]) -> list[dict[str, any]]:  # type: ignore[valid-type]
    """Filter out OS metadata files from a list of file info dicts.

    Args:
        files: List of file info dictionaries with 'path' key

    Returns:
        Filtered list with OS metadata files removed

    Examples:
        >>> files = [{"path": "file.txt"}, {"path": "._meta"}]
        >>> filter_os_metadata_dicts(files)
        [{'path': 'file.txt'}]
    """
    return [f for f in files if not is_os_metadata_file(f.get("path", ""))]
