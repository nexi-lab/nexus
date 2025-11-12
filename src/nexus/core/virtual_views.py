"""Virtual view support for file parsing (_parsed suffix pattern).

This module provides shared logic for creating virtual views of binary files
as text. When a user requests `file_parsed.xlsx.md`, the system:
1. Recognizes it as a virtual view request
2. Reads the original `file.xlsx`
3. Parses it using the appropriate parser (MarkItDown)
4. Returns the parsed text content

Virtual views are read-only and don't create actual files.

Naming convention:
- Original file: `file.xlsx` → always returns binary
- Parsed view: `file_parsed.xlsx.md` → returns parsed markdown

Safety features:
- Only creates views for files that exist
- Only applies to parseable file types
- Works consistently across FUSE and RPC layers
- Binary files always return binary (no auto-parsing)
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any, overload

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


def parse_virtual_path(path: str, exists_fn: Callable[[str], bool]) -> tuple[str, str | None]:
    """Parse virtual path to extract original path and view type.

    Args:
        path: Virtual path (e.g., "/file_parsed.xlsx.md" or "/document_parsed.pdf.md")
        exists_fn: Function to check if a path exists

    Returns:
        Tuple of (original_path, view_type)
        - original_path: Original file path without virtual suffix
        - view_type: "md" or None for raw/binary access

    Examples:
        >>> parse_virtual_path("/file_parsed.xlsx.md", exists_fn)
        ("/file.xlsx", "md")
        >>> parse_virtual_path("/file.txt", exists_fn)
        ("/file.txt", None)  # Actual .txt file, not a virtual view
        >>> parse_virtual_path("/file_parsed.xlsx", exists_fn)
        ("/file_parsed.xlsx", None)  # Missing .md extension
    """
    # Handle _parsed.{ext}.md virtual views
    # Pattern: file_parsed.{ext}.md → file.{ext}
    # Only treat as virtual view if:
    # 1. File ends with .md
    # 2. Contains _parsed before the original extension
    # 3. The file without _parsed.md suffix actually exists
    if path.endswith(".md"):
        # Find the last occurrence of _parsed in the path
        # e.g., "/dir/file_parsed.xlsx.md" → find "_parsed" before ".xlsx.md"
        parsed_idx = path.rfind("_parsed.")

        if parsed_idx != -1:
            # Extract the base name and extension
            # e.g., "/dir/file_parsed.xlsx.md" → "/dir/file" + ".xlsx"
            base_path = path[:parsed_idx]  # Everything before "_parsed"
            ext_with_md = path[
                parsed_idx + 7 :
            ]  # Everything after "_parsed" (skip 7 chars to keep the dot)

            # Remove the .md suffix to get the original extension
            # e.g., ".xlsx.md" → ".xlsx"
            if ext_with_md.endswith(".md"):
                original_ext = ext_with_md[:-3]  # Remove .md
                original_path = base_path + original_ext

                # Check if the original file exists
                if exists_fn(original_path):
                    return (original_path, "md")

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
    # Check if this is a parseable binary file (Excel, PDF, etc.)
    # For these files, we should use the parser directly, not try to decode as UTF-8
    is_parseable = any(path.endswith(ext) for ext in PARSEABLE_EXTENSIONS)

    if is_parseable:
        # Use parser for parseable binary files (Excel, PDF, Word, etc.)
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
    else:
        # For non-parseable files, try to decode as text first
        try:
            decoded_content = content.decode("utf-8")
            return decoded_content.encode("utf-8")
        except UnicodeDecodeError:
            # If decode fails, this is likely a binary file we don't know how to parse
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
    if file_path.endswith(".md"):
        return False

    # Don't add virtual views to files that already have _parsed in the name
    if "_parsed." in file_path:
        return False

    # Only add virtual views for parseable file types
    return any(file_path.endswith(ext) for ext in PARSEABLE_EXTENSIONS)


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

    virtual_files: list[str] | list[dict[str, Any]] = []

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
        except Exception:
            pass

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
                    virtual_files.append(parsed_path)  # type: ignore[arg-type]
                else:
                    # For dict format, create a copy with modified path
                    parsed_file = file.copy()
                    parsed_file["path"] = parsed_path
                    virtual_files.append(parsed_file)  # type: ignore[arg-type]

    return files + virtual_files  # type: ignore[operator]
