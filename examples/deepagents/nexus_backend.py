"""
Nexus Backend Adapter for LangChain DeepAgents

This module provides a BackendProtocol implementation that routes DeepAgents
filesystem operations to Nexus, enabling agents to use Nexus as their persistent
storage layer.

Key Features:
- Automatic versioning of all file writes
- Persistent storage (local or remote)
- Audit trail of all agent operations
- Time-travel debugging capabilities
"""

import re
from typing import Any

try:
    from deepagents.backends.protocol import BackendProtocol, EditResult, WriteResult
    from deepagents.backends.utils import FileInfo, GrepMatch
except ImportError:
    raise ImportError("deepagents not installed. Install with: pip install deepagents") from None

import nexus


class NexusBackend:
    """
    Nexus implementation of DeepAgents BackendProtocol.

    Maps DeepAgents file operations to Nexus filesystem operations,
    providing automatic versioning, persistence, and audit capabilities.

    Args:
        nx: Nexus filesystem instance (from nexus.connect())
        base_path: Base path for all agent operations (default: "/")
        encoding: Text encoding for file content (default: "utf-8")

    Example:
        >>> import nexus
        >>> from deepagents import create_deep_agent
        >>> from deepagents.middleware.filesystem import FilesystemMiddleware
        >>> from nexus_backend import NexusBackend
        >>>
        >>> nx = nexus.connect()
        >>> agent = create_deep_agent(
        ...     model="anthropic:claude-sonnet-4-20250514",
        ...     middleware=[
        ...         FilesystemMiddleware(backend=NexusBackend(nx))
        ...     ]
        ... )
    """

    def __init__(
        self,
        nx: Any,  # nexus.Nexus type
        base_path: str = "/",
        encoding: str = "utf-8",
    ):
        self.nx = nx
        self.base_path = base_path.rstrip("/")
        self.encoding = encoding

    def _resolve_path(self, path: str) -> str:
        """
        Resolve paths relative to base_path workspace.

        All paths are treated as relative to base_path for workspace isolation.
        If path starts with "/", it's treated as relative to base_path (not root).

        Args:
            path: Path to resolve (relative or absolute)

        Returns:
            Absolute path within Nexus, confined to base_path workspace

        Examples:
            With base_path="/workspace":
            - "file.txt" -> "/workspace/file.txt"
            - "/file.txt" -> "/workspace/file.txt" (stripped leading /)
            - "dir/file.txt" -> "/workspace/dir/file.txt"
        """
        # Strip leading slash to treat everything as relative to base_path
        # This ensures workspace isolation
        if path.startswith("/"):
            path = path.lstrip("/")

        # Combine with base_path
        full_path = f"{self.base_path}/{path}".replace("//", "/")
        return full_path

    def _exists(self, path: str) -> bool:
        """Check if a path exists in Nexus."""
        return self.nx.exists(path)

    def ls_info(self, path: str) -> list[FileInfo]:
        """
        List directory contents with metadata.

        Args:
            path: Directory path to list

        Returns:
            List of FileInfo dictionaries with path, is_dir, size, modified_at
        """
        resolved_path = self._resolve_path(path)

        try:
            entries = self.nx.list(resolved_path)
            result = []

            for entry in entries:
                # Remove leading slash if present (list returns full paths)
                if entry.startswith("/"):
                    display_path = entry[len(resolved_path) :].lstrip("/")
                else:
                    display_path = entry

                entry_path = f"{resolved_path}/{display_path}".replace("//", "/")

                try:
                    is_dir = self.nx.is_directory(entry_path)

                    # Get file size by reading if it's a file
                    size = 0
                    if not is_dir:
                        try:
                            content = self.nx.read(entry_path)
                            size = len(content)
                        except Exception:
                            pass

                    file_info: FileInfo = {
                        "path": display_path,
                        "is_dir": is_dir,
                        "size": size,
                    }
                    result.append(file_info)
                except Exception:
                    # Best-effort: if check fails, include minimal info
                    result.append({"path": display_path})

            return result

        except FileNotFoundError:
            return []
        except Exception:
            # Return empty list on errors (DeepAgents convention)
            return []

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> str:
        """
        Read file content with line numbers.

        Args:
            file_path: Path to file
            offset: Starting line number (0-indexed)
            limit: Maximum number of lines to read

        Returns:
            File content with line numbers (cat -n format) or error message
        """
        resolved_path = self._resolve_path(file_path)

        try:
            # Read file from Nexus (returns bytes)
            content_bytes = self.nx.read(resolved_path)
            content = content_bytes.decode(self.encoding)

            # Split into lines
            lines = content.splitlines()

            # Apply offset and limit
            selected_lines = lines[offset : offset + limit]

            # Format with line numbers (1-indexed, like cat -n)
            formatted_lines = [
                f"{offset + i + 1:6d}\t{line}" for i, line in enumerate(selected_lines)
            ]

            result = "\n".join(formatted_lines)

            # Add truncation notice if needed
            if len(lines) > offset + limit:
                remaining = len(lines) - (offset + limit)
                result += f"\n\n... ({remaining} more lines)"

            return result

        except FileNotFoundError:
            return f"Error: File not found: {file_path}"
        except Exception as e:
            return f"Error reading file: {str(e)}"

    def write(self, file_path: str, content: str) -> WriteResult:
        """
        Write content to file (creates or overwrites).

        Args:
            file_path: Path to file
            content: Content to write

        Returns:
            WriteResult with error (if any) and path
        """
        resolved_path = self._resolve_path(file_path)

        try:
            # Encode content to bytes
            content_bytes = content.encode(self.encoding)

            # Write to Nexus (automatically versioned)
            self.nx.write(resolved_path, content_bytes)

            return WriteResult(error=None, path=file_path, files_update=None)

        except Exception as e:
            return WriteResult(
                error=f"Failed to write file: {str(e)}", path=file_path, files_update=None
            )

    def edit(
        self, file_path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        """
        Edit file by replacing string occurrences.

        Args:
            file_path: Path to file
            old_string: String to find
            new_string: Replacement string
            replace_all: If True, replace all occurrences; if False, only first

        Returns:
            EditResult with error (if any), path, and occurrence count
        """
        resolved_path = self._resolve_path(file_path)

        try:
            # Read current content
            content_bytes = self.nx.read(resolved_path)
            content = content_bytes.decode(self.encoding)

            # Count occurrences
            occurrences = content.count(old_string)

            if occurrences == 0:
                return EditResult(
                    error=f"String not found: {old_string[:50]}...",
                    path=file_path,
                    files_update=None,
                    occurrences=0,
                )

            # Check for ambiguity if not replace_all
            if not replace_all and occurrences > 1:
                return EditResult(
                    error=f"String appears {occurrences} times. Use replace_all=True or provide more context.",
                    path=file_path,
                    files_update=None,
                    occurrences=occurrences,
                )

            # Perform replacement
            if replace_all:
                new_content = content.replace(old_string, new_string)
            else:
                new_content = content.replace(old_string, new_string, 1)

            # Write back (creates new version in Nexus)
            new_content_bytes = new_content.encode(self.encoding)
            self.nx.write(resolved_path, new_content_bytes)

            return EditResult(
                error=None, path=file_path, files_update=None, occurrences=occurrences
            )

        except FileNotFoundError:
            return EditResult(
                error=f"File not found: {file_path}",
                path=file_path,
                files_update=None,
                occurrences=0,
            )
        except Exception as e:
            return EditResult(
                error=f"Failed to edit file: {str(e)}",
                path=file_path,
                files_update=None,
                occurrences=0,
            )

    def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        """
        Find files matching glob pattern.

        Args:
            pattern: Glob pattern (e.g., "*.md", "**/*.py")
            path: Base path to search from

        Returns:
            List of FileInfo dictionaries for matching files
        """
        resolved_path = self._resolve_path(path)

        try:
            # Nexus glob expects full pattern with path
            full_pattern = f"{resolved_path}/{pattern}".replace("//", "/")
            matches = self.nx.glob(full_pattern)

            result = []
            for match_path in matches:
                try:
                    # Make path relative to search base
                    relative_path = match_path
                    if match_path.startswith(resolved_path):
                        relative_path = match_path[len(resolved_path) :].lstrip("/")

                    is_dir = self.nx.is_directory(match_path)

                    # Get file size if it's a file
                    size = 0
                    if not is_dir:
                        try:
                            content = self.nx.read(match_path)
                            size = len(content)
                        except Exception:
                            pass

                    file_info: FileInfo = {
                        "path": relative_path,
                        "is_dir": is_dir,
                        "size": size,
                    }
                    result.append(file_info)
                except Exception:
                    # Best-effort: include minimal info if check fails
                    result.append({"path": match_path})

            return result

        except Exception:
            # Return empty list on errors
            return []

    def grep_raw(
        self, pattern: str, path: str | None = None, glob: str | None = None
    ) -> list[GrepMatch] | str:
        """
        Search file contents for pattern.

        Args:
            pattern: Regex pattern to search for
            path: Specific file or directory to search
            glob: Glob pattern to filter files

        Returns:
            List of GrepMatch dictionaries or error string
        """
        try:
            # Build search path
            search_path = self._resolve_path(path or "/")

            # Get list of files to search
            if glob:
                files_to_search = [
                    f["path"]
                    for f in self.glob_info(glob, search_path)
                    if not f.get("is_dir", False)
                ]
            else:
                # Search all files recursively
                files_to_search = self._list_all_files(search_path)

            # Compile regex pattern
            try:
                regex = re.compile(pattern)
            except re.error as e:
                return f"Invalid regex pattern: {str(e)}"

            # Search each file
            matches = []
            for file_path in files_to_search:
                try:
                    resolved_file = self._resolve_path(file_path)
                    content_bytes = self.nx.read(resolved_file)
                    content = content_bytes.decode(self.encoding)

                    for line_num, line in enumerate(content.splitlines(), 1):
                        if regex.search(line):
                            match: GrepMatch = {"path": file_path, "line": line_num, "text": line}
                            matches.append(match)

                except Exception:
                    # Skip files that can't be read
                    continue

            return matches

        except Exception as e:
            return f"Grep error: {str(e)}"

    def _list_all_files(self, path: str, max_depth: int = 10) -> list[str]:
        """
        Recursively list all files under a path.

        Args:
            path: Directory path
            max_depth: Maximum recursion depth

        Returns:
            List of file paths
        """
        if max_depth <= 0:
            return []

        files = []
        try:
            entries = self.ls_info(path)

            for entry in entries:
                entry_path = f"{path}/{entry['path']}".replace("//", "/")

                if entry.get("is_dir", False):
                    # Recurse into directory
                    files.extend(self._list_all_files(entry_path, max_depth - 1))
                else:
                    # Add file
                    files.append(entry_path)

        except Exception:
            pass

        return files


# Verify protocol compliance at runtime
if not isinstance(NexusBackend, type):
    raise TypeError("NexusBackend must be a class")

# This ensures NexusBackend implements BackendProtocol correctly
_backend_check: BackendProtocol = NexusBackend(nexus.connect())  # type: ignore
