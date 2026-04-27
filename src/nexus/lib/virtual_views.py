"""Virtual view support for file parsing (_parsed suffix pattern).

Tier-neutral helper that lives in ``nexus.lib`` because it is used by
both kernel (``nexus.core``) and presentation (``nexus.fuse``) layers.

When a user requests ``file_parsed.xlsx.md``, the system:
1. Recognizes it as a virtual view request
2. Reads the original ``file.xlsx``
3. Parses it using the appropriate parser (pdf-inspector)
4. Returns the parsed text content

Virtual views are read-only and don't create actual files.

Naming convention:
- Original file: ``file.xlsx`` → always returns binary
- Parsed view: ``file_parsed.xlsx.md`` → returns parsed markdown

Safety features:
- Only creates views for files that exist
- Only applies to parseable file types
- Works consistently across FUSE and RPC layers
- Binary files always return binary (no auto-parsing)

History:
    nexus.core.virtual_views → nexus.lib.virtual_views
"""

import logging
import re
from collections.abc import Callable
from typing import Any, cast, overload

logger = logging.getLogger(__name__)

# File extensions that support parsing to text
# Note: Image formats (.jpg, .jpeg, .png) require OCR which is not enabled by default,
# so they are excluded from automatic virtual view generation
PARSEABLE_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".doc",
    ".xlsx",
    ".xls",
    ".pptx",
    ".ppt",
    ".odt",
    ".ods",
    ".odp",
    ".rtf",
    ".epub",
}

_PARSED_VIEW_SUFFIX_RE = re.compile(r"_parsed\.(?P<ext>[^/.]+)\.md$", re.IGNORECASE)


def is_parseable_path(path: str) -> bool:
    """Case-insensitive membership test against ``PARSEABLE_EXTENSIONS``.

    Real filenames arrive with mixed casing (``Report.PDF``, ``Deck.Docx``);
    naïve ``path.endswith(".pdf")`` misses them, which means the parse-aware
    indexer silently falls back to raw-byte decoding.  Use this helper
    everywhere parseable detection matters.
    """
    lower = path.lower()
    return any(lower.endswith(ext) for ext in PARSEABLE_EXTENSIONS)


def parse_virtual_path(path: str, check_fn: Callable[[str], Any]) -> tuple[str, str | None, Any]:
    """Parse virtual path to extract original path, view type, and check result.

    Args:
        path: Virtual path (e.g., "/file_parsed.xlsx.md" or "/document_parsed.pdf.md")
        check_fn: Function to verify the original file exists.  Can be:
            - ``metadata.exists`` (returns bool) — for simple existence checks
            - ``metadata.get``   (returns FileMetadata | None) — when the caller
              needs the metadata anyway (avoids a redundant second lookup)
            The result is tested for truthiness and passed through as the
            third element of the return tuple.

    Returns:
        Tuple of (original_path, view_type, check_result)
        - original_path: Original file path without virtual suffix
        - view_type: "md" or None for raw/binary access
        - check_result: Return value of check_fn(original_path), or None
          when the path is not a virtual view

    Examples:
        >>> parse_virtual_path("/file_parsed.xlsx.md", exists_fn)
        ("/file.xlsx", "md", True)
        >>> parse_virtual_path("/file.txt", exists_fn)
        ("/file.txt", None, None)
    """
    # Handle _parsed.{ext}.md virtual views
    # Pattern: file_parsed.{ext}.md → file.{ext}
    # Only treat as virtual view if:
    # 1. File ends with .md
    # 2. Contains _parsed before the original extension
    # 3. The file without _parsed.md suffix actually exists
    match = _PARSED_VIEW_SUFFIX_RE.search(path)
    if match is not None:
        original_ext = f".{match.group('ext')}"
        original_path = f"{path[: match.start()]}{original_ext}"

        # Only treat as virtual view if the extension is parseable
        # and the original file exists.
        if original_ext.lower() in PARSEABLE_EXTENSIONS:
            result = check_fn(original_path)
            if result:
                return (original_path, "md", result)

    # Not a virtual view, return as-is
    return (path, None, None)


def get_parsed_content(
    content: bytes,
    path: str,
    view_type: str,  # noqa: ARG001
    parse_fn: Callable[[bytes, str], bytes | None] | None = None,
) -> bytes:
    """Get parsed content for a file.

    Args:
        content: Raw file content as bytes
        path: Original file path (for parser detection)
        view_type: View type ("txt" or "md") - reserved for future use
        parse_fn: Optional callback ``(content, path) -> parsed_bytes | None``.
            Provided by the caller so that core/ does not import from parsers/.
            When *None* and the file is parseable, raw content is returned.

    Returns:
        Parsed content as bytes (UTF-8 encoded text)
    """
    # Check if this is a parseable binary file (Excel, PDF, etc.)
    is_parseable = is_parseable_path(path)

    if is_parseable:
        if parse_fn is not None:
            try:
                result = parse_fn(content, path)
                if result is not None:
                    return result
            except Exception as e:
                logger.warning(f"Error parsing file {path}: {e}")
        else:
            logger.debug(f"No parse_fn provided for {path}, returning raw content")
    else:
        # For non-parseable files, try to decode as text first
        try:
            decoded_content = content.decode("utf-8")
            return decoded_content.encode("utf-8")
        except UnicodeDecodeError:
            pass

    # Fallback to raw content if parsing fails
    return content


def should_add_virtual_views(file_path: str) -> bool:
    """Check if a file should have a virtual _parsed.{ext}.md view added.

    Args:
        file_path: File path to check

    Returns:
        True if virtual views should be added

    Examples:
        >>> should_add_virtual_views("/file.xlsx")
        True
        >>> should_add_virtual_views("/file.txt")
        False  # Already a text file
        >>> should_add_virtual_views("/file_parsed.xlsx.md")
        False  # Already a virtual view
        >>> should_add_virtual_views("/file.unknown")
        False  # Not a parseable type
    """
    # Don't add virtual views to files that already end with .md
    lower_path = file_path.lower()
    if lower_path.endswith(".md"):
        return False

    # Don't add virtual views to files that already have _parsed in the name
    filename = lower_path.rsplit("/", 1)[-1]
    if "_parsed." in filename:
        return False

    # Only add virtual views for parseable file types
    return is_parseable_path(file_path)


@overload
def add_virtual_views_to_listing(
    files: list[str],
    is_directory_fn: Callable[[str], bool],
    show_parsed: bool = True,
) -> list[str]: ...


@overload
def add_virtual_views_to_listing(
    files: list[dict[str, Any]],
    is_directory_fn: Callable[[str], bool],
    show_parsed: bool = True,
) -> list[dict[str, Any]]: ...


def add_virtual_views_to_listing(
    files: list[str] | list[dict[str, Any]],
    is_directory_fn: Callable[[str], bool],
    show_parsed: bool = True,
) -> list[str] | list[dict[str, Any]]:
    """Add virtual _parsed.{ext}.md views to a file listing.

    Args:
        files: List of file paths (strings) or file dicts with "path" key
        is_directory_fn: Function to check if a path is a directory
        show_parsed: If True, include parsed virtual views in the listing (default: True)

    Returns:
        Updated list with virtual views added (if show_parsed=True)

    Examples:
        >>> files = ["/file.xlsx", "/file.txt", "/dir/"]
        >>> add_virtual_views_to_listing(files, is_dir_fn, show_parsed=True)
        ["/file.xlsx", "/file_parsed.xlsx.md", "/file.txt", "/dir/"]
        >>> add_virtual_views_to_listing(files, is_dir_fn, show_parsed=False)
        ["/file.xlsx", "/file.txt", "/dir/"]
    """
    # If show_parsed is False, don't add virtual views
    if not show_parsed:
        return files

    virtual_files: list[Any] = []

    for file in files:
        # Get the file path (handle both string and dict formats)
        if isinstance(file, str):
            file_path = file
            is_dir = None  # Unknown, will need to check
        elif isinstance(file, dict) and "path" in file:
            file_path = file["path"]
            # OPTIMIZATION: Use is_directory from dict if available (avoids N RPC calls)
            is_dir = file.get("is_directory", None)
        else:
            continue

        # Skip directories
        # First check if we already know from the dict, then fall back to function call
        try:
            if is_dir is None:
                # Only call is_directory_fn if we don't already know
                if is_directory_fn(file_path):
                    continue
            elif is_dir:
                # Already know it's a directory from the dict
                continue
        except Exception as e:
            logger.debug("Error checking directory status for %s: %s", file_path, e)

        # Check if we should add virtual views
        if should_add_virtual_views(file_path):
            # Extract the file extension and create the _parsed.{ext}.md name
            # e.g., "/file.xlsx" → "/file_parsed.xlsx.md"
            # Find the last dot to get the extension
            last_dot = file_path.rfind(".")
            if last_dot != -1:
                base_name = file_path[:last_dot]
                extension = file_path[last_dot:]
                parsed_path = f"{base_name}_parsed{extension}.md"

                if isinstance(file, str):
                    virtual_files.append(parsed_path)
                else:
                    # For dict format, create a copy with modified path
                    parsed_file = file.copy()
                    parsed_file["path"] = parsed_path
                    virtual_files.append(parsed_file)

    return cast("list[str] | list[dict[str, Any]]", [*files, *virtual_files])
