"""Virtual view support for file parsing (.txt and .md suffixes).

This module provides shared logic for creating virtual views of binary files
as text. When a user requests `file.xlsx.txt`, the system:
1. Recognizes it as a virtual view request
2. Reads the original `file.xlsx`
3. Parses it using the appropriate parser (MarkItDown)
4. Returns the parsed text content

Virtual views are read-only and don't create actual files.

Safety features:
- Prevents double suffixes (no .txt.txt or .md.md)
- Only creates views for files that exist
- Only applies to parseable file types
- Works consistently across FUSE and RPC layers
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any, overload

logger = logging.getLogger(__name__)

# File extensions that support parsing to text
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
    ".jpg",
    ".jpeg",
    ".png",
}


def parse_virtual_path(path: str, exists_fn: Callable[[str], bool]) -> tuple[str, str | None]:
    """Parse virtual path to extract original path and view type.

    Args:
        path: Virtual path (e.g., "/file.pdf.txt" or "/file.xlsx.md")
        exists_fn: Function to check if a path exists

    Returns:
        Tuple of (original_path, view_type)
        - original_path: Original file path without virtual suffix
        - view_type: "txt", "md", or None for raw/binary access

    Examples:
        >>> parse_virtual_path("/file.xlsx.txt", exists_fn)
        ("/file.xlsx", "txt")
        >>> parse_virtual_path("/file.txt", exists_fn)
        ("/file.txt", None)  # Actual .txt file, not a virtual view
        >>> parse_virtual_path("/file.xlsx.txt.txt", exists_fn)
        ("/file.xlsx.txt.txt", None)  # Prevents double suffixes
    """
    # Handle .txt virtual views
    # Only treat as virtual view if:
    # 1. File ends with .txt
    # 2. Doesn't end with .txt.txt (prevent double suffixes)
    # 3. The file without .txt extension actually exists
    if path.endswith(".txt") and not path.endswith(".txt.txt"):
        base_path = path[:-4]  # Remove .txt suffix
        # Check if base file exists (this creates a virtual view)
        if exists_fn(base_path):
            return (base_path, "txt")

    # Handle .md virtual views
    # Only treat as virtual view if:
    # 1. File ends with .md
    # 2. Doesn't end with .md.md (prevent double suffixes)
    # 3. The file without .md extension actually exists
    elif path.endswith(".md") and not path.endswith(".md.md"):
        base_path = path[:-3]  # Remove .md suffix
        # Check if base file exists (this creates a virtual view)
        if exists_fn(base_path):
            return (base_path, "md")

    # Not a virtual view, return as-is
    return (path, None)


def get_parsed_content(content: bytes, path: str, view_type: str) -> bytes:  # noqa: ARG001
    """Get parsed content for a file.

    Args:
        content: Raw file content as bytes
        path: Original file path (for parser detection)
        view_type: View type ("txt" or "md") - reserved for future use

    Returns:
        Parsed content as bytes (UTF-8 encoded text)

    Raises:
        Exception: If parsing fails (falls back to raw content)
    """
    # Try to decode as text first
    try:
        decoded_content = content.decode("utf-8")
        return decoded_content.encode("utf-8")
    except UnicodeDecodeError:
        # Use parser for non-text files
        from nexus.parsers import MarkItDownParser, ParserRegistry, prepare_content_for_parsing

        try:
            # Prepare content
            processed_content, effective_path, metadata = prepare_content_for_parsing(content, path)

            # Get parser - need to register MarkItDownParser
            registry = ParserRegistry()
            registry.register(MarkItDownParser())
            parser = registry.get_parser(effective_path)

            if parser:
                # Parse synchronously (works in both sync and async contexts)
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                result = loop.run_until_complete(parser.parse(processed_content, metadata))

                if result and result.text:
                    return result.text.encode("utf-8")

        except Exception as e:
            # Log parser errors but don't fail - fall back to raw content
            from nexus.core.exceptions import ParserError

            if isinstance(e, ParserError):
                logger.debug(f"No parser available for {path}, using raw content")
            else:
                logger.warning(f"Error parsing file {path}: {e}")

    # Fallback to raw content if parsing fails
    return content


def should_add_virtual_views(file_path: str) -> bool:
    """Check if a file should have virtual .txt and .md views added.

    Args:
        file_path: File path to check

    Returns:
        True if virtual views should be added

    Examples:
        >>> should_add_virtual_views("/file.xlsx")
        True
        >>> should_add_virtual_views("/file.txt")
        False  # Already a text file
        >>> should_add_virtual_views("/file.unknown")
        False  # Not a parseable type
    """
    # Don't add virtual views to files that already end with .txt or .md
    if file_path.endswith((".txt", ".md")):
        return False

    # Only add virtual views for parseable file types
    return any(file_path.endswith(ext) for ext in PARSEABLE_EXTENSIONS)


@overload
def add_virtual_views_to_listing(
    files: list[str],
    is_directory_fn: Callable[[str], bool],
) -> list[str]: ...


@overload
def add_virtual_views_to_listing(
    files: list[dict[str, Any]],
    is_directory_fn: Callable[[str], bool],
) -> list[dict[str, Any]]: ...


def add_virtual_views_to_listing(
    files: list[str] | list[dict[str, Any]],
    is_directory_fn: Callable[[str], bool],
) -> list[str] | list[dict[str, Any]]:
    """Add virtual .txt and .md views to a file listing.

    Args:
        files: List of file paths (strings) or file dicts with "path" key
        is_directory_fn: Function to check if a path is a directory

    Returns:
        Updated list with virtual views added

    Examples:
        >>> files = ["/file.xlsx", "/file.txt", "/dir/"]
        >>> add_virtual_views_to_listing(files, is_dir_fn)
        ["/file.xlsx", "/file.xlsx.txt", "/file.xlsx.md", "/file.txt", "/dir/"]
    """
    virtual_files: list[str] | list[dict[str, Any]] = []

    for file in files:
        # Get the file path (handle both string and dict formats)
        if isinstance(file, str):
            file_path = file
        elif isinstance(file, dict) and "path" in file:
            file_path = file["path"]
        else:
            continue

        # Skip directories
        try:
            if is_directory_fn(file_path):
                continue
        except Exception:
            pass

        # Check if we should add virtual views
        if should_add_virtual_views(file_path):
            # Add .txt and .md virtual views
            if isinstance(file, str):
                virtual_files.append(f"{file_path}.txt")  # type: ignore[arg-type]
                virtual_files.append(f"{file_path}.md")  # type: ignore[arg-type]
            else:
                # For dict format, create copies with modified path
                txt_file = file.copy()
                txt_file["path"] = f"{file_path}.txt"
                virtual_files.append(txt_file)  # type: ignore[arg-type]

                md_file = file.copy()
                md_file["path"] = f"{file_path}.md"
                virtual_files.append(md_file)  # type: ignore[arg-type]

    return files + virtual_files  # type: ignore[operator]
