"""Embedded mode implementation for Nexus."""

from __future__ import annotations

import builtins
import contextlib
import fnmatch
import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from nexus.backends.backend import Backend
from nexus.backends.local import LocalBackend
from nexus.core.exceptions import InvalidPathError, NexusFileNotFoundError
from nexus.core.filesystem import NexusFilesystem
from nexus.core.metadata import FileMetadata
from nexus.core.router import NamespaceConfig, PathRouter
from nexus.storage.metadata_store import SQLAlchemyMetadataStore


class Embedded(NexusFilesystem):
    """
    Embedded mode filesystem for Nexus.

    Provides file operations (read, write, delete) with metadata tracking
    using content-addressable storage (CAS) for automatic deduplication.

    All backends now use CAS by default for:
    - Automatic deduplication (same content stored once)
    - Content integrity (hash verification)
    - Efficient storage
    """

    def __init__(
        self,
        data_dir: str | Path = "./nexus-data",
        db_path: str | Path | None = None,
        tenant_id: str | None = None,
        agent_id: str | None = None,
        is_admin: bool = False,
        custom_namespaces: list[NamespaceConfig] | None = None,
        enable_metadata_cache: bool = True,
        cache_path_size: int = 512,
        cache_list_size: int = 128,
        cache_kv_size: int = 256,
        cache_exists_size: int = 1024,
        cache_ttl_seconds: int | None = 300,
    ):
        """
        Initialize embedded filesystem.

        Args:
            data_dir: Root directory for storing files
            db_path: Path to SQLite metadata database (auto-generated if None)
            tenant_id: Tenant identifier for multi-tenant isolation (optional)
            agent_id: Agent identifier for agent-level isolation in /workspace (optional)
            is_admin: Whether this instance has admin privileges (default: False)
            custom_namespaces: Additional custom namespace configurations (optional)
            enable_metadata_cache: Enable in-memory metadata caching (default: True)
            cache_path_size: Max entries for path metadata cache (default: 512)
            cache_list_size: Max entries for directory listing cache (default: 128)
            cache_kv_size: Max entries for file metadata KV cache (default: 256)
            cache_exists_size: Max entries for existence check cache (default: 1024)
            cache_ttl_seconds: Cache TTL in seconds, None = no expiry (default: 300)
        """
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Store tenant and agent context
        self.tenant_id = tenant_id
        self.agent_id = agent_id
        self.is_admin = is_admin

        # Initialize metadata store (using new SQLAlchemy-based store)
        if db_path is None:
            db_path = self.data_dir / "metadata.db"
        self.metadata = SQLAlchemyMetadataStore(
            db_path=db_path,
            enable_cache=enable_metadata_cache,
            cache_path_size=cache_path_size,
            cache_list_size=cache_list_size,
            cache_kv_size=cache_kv_size,
            cache_exists_size=cache_exists_size,
            cache_ttl_seconds=cache_ttl_seconds,
        )

        # Initialize path router with default namespaces
        self.router = PathRouter()

        # Register custom namespaces if provided
        if custom_namespaces:
            for ns_config in custom_namespaces:
                self.router.register_namespace(ns_config)

        # Initialize unified backend (always uses CAS)
        self.backend: Backend = LocalBackend(self.data_dir)
        self.router.add_mount("/", self.backend, priority=0)

    def _validate_path(self, path: str) -> str:
        """
        Validate virtual path.

        Args:
            path: Virtual path to validate

        Returns:
            Normalized path

        Raises:
            InvalidPathError: If path is invalid
        """
        if not path:
            raise InvalidPathError("", "Path cannot be empty")

        # Ensure path starts with /
        if not path.startswith("/"):
            path = "/" + path

        # Check for invalid characters
        invalid_chars = ["\0", "\n", "\r"]
        for char in invalid_chars:
            if char in path:
                raise InvalidPathError(path, f"Path contains invalid character: {repr(char)}")

        # Check for parent directory traversal
        if ".." in path:
            raise InvalidPathError(path, "Path contains '..' segments")

        return path

    def _compute_etag(self, content: bytes) -> str:
        """
        Compute ETag for file content.

        Args:
            content: File content

        Returns:
            ETag (MD5 hash)
        """
        return hashlib.md5(content).hexdigest()

    def read(self, path: str) -> bytes:
        """
        Read file content as bytes.

        Args:
            path: Virtual path to read

        Returns:
            File content as bytes

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            BackendError: If read operation fails
            AccessDeniedError: If access is denied based on tenant isolation
        """
        path = self._validate_path(path)

        # Route to backend with access control
        route = self.router.route(
            path,
            tenant_id=self.tenant_id,
            agent_id=self.agent_id,
            is_admin=self.is_admin,
            check_write=False,
        )

        # Check if file exists in metadata
        meta = self.metadata.get(path)
        if meta is None or meta.etag is None:
            raise NexusFileNotFoundError(path)

        # Read from routed backend using content hash
        content = route.backend.read_content(meta.etag)

        return content

    def write(self, path: str, content: bytes) -> None:
        """
        Write content to a file.

        Creates parent directories if needed. Overwrites existing files.
        Updates metadata store.

        Automatically deduplicates content using CAS.

        Args:
            path: Virtual path to write
            content: File content as bytes

        Raises:
            InvalidPathError: If path is invalid
            BackendError: If write operation fails
            AccessDeniedError: If access is denied (tenant isolation or read-only namespace)
            PermissionError: If path is read-only
        """
        path = self._validate_path(path)

        # Route to backend with write access check
        route = self.router.route(
            path,
            tenant_id=self.tenant_id,
            agent_id=self.agent_id,
            is_admin=self.is_admin,
            check_write=True,
        )

        # Check if path is read-only
        if route.readonly:
            raise PermissionError(f"Path is read-only: {path}")

        # Get existing metadata for update detection
        now = datetime.now(UTC)
        meta = self.metadata.get(path)

        # Write to routed backend - returns content hash
        content_hash = route.backend.write_content(content)

        # If updating existing file with different content, delete old content
        if meta is not None and meta.etag and meta.etag != content_hash:
            # Decrement ref count for old content
            with contextlib.suppress(Exception):
                # Ignore errors if old content already deleted
                route.backend.delete_content(meta.etag)

        # Store metadata with content hash as both etag and physical_path
        metadata = FileMetadata(
            path=path,
            backend_name="local",
            physical_path=content_hash,  # CAS: hash is the "physical" location
            size=len(content),
            etag=content_hash,  # SHA-256 hash for integrity
            created_at=meta.created_at if meta else now,
            modified_at=now,
            version=1,
        )

        self.metadata.put(metadata)

    def delete(self, path: str) -> None:
        """
        Delete a file.

        Removes file from backend and metadata store.
        Decrements reference count in CAS (only deletes when ref_count=0).

        Args:
            path: Virtual path to delete

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            BackendError: If delete operation fails
            AccessDeniedError: If access is denied (tenant isolation or read-only namespace)
            PermissionError: If path is read-only
        """
        path = self._validate_path(path)

        # Route to backend with write access check (delete requires write permission)
        route = self.router.route(
            path,
            tenant_id=self.tenant_id,
            agent_id=self.agent_id,
            is_admin=self.is_admin,
            check_write=True,
        )

        # Check if path is read-only
        if route.readonly:
            raise PermissionError(f"Cannot delete from read-only path: {path}")

        # Check if file exists in metadata
        meta = self.metadata.get(path)
        if meta is None:
            raise NexusFileNotFoundError(path)

        # Delete from routed backend CAS (decrements ref count)
        if meta.etag:
            route.backend.delete_content(meta.etag)

        # Remove from metadata
        self.metadata.delete(path)

    def exists(self, path: str) -> bool:
        """
        Check if a file exists.

        Args:
            path: Virtual path to check

        Returns:
            True if file exists, False otherwise
        """
        try:
            path = self._validate_path(path)
            return self.metadata.exists(path)
        except InvalidPathError:
            return False

    def list(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        prefix: str | None = None,
    ) -> builtins.list[str] | builtins.list[dict[str, Any]]:
        """
        List files in a directory.

        Args:
            path: Directory path to list (default: "/")
            recursive: If True, list all files recursively; if False, list only direct children (default: True)
            details: If True, return detailed metadata; if False, return paths only (default: False)
            prefix: (Deprecated) Path prefix to filter by - for backward compatibility.
                    When used, lists all files recursively with this prefix.

        Returns:
            List of file paths (if details=False) or list of file metadata dicts (if details=True).
            Each metadata dict contains: path, size, modified_at, etag

        Examples:
            # List all files recursively (default)
            fs.list()  # Returns: ["/file1.txt", "/dir/file2.txt", "/dir/subdir/file3.txt"]

            # List files in root directory only (non-recursive)
            fs.list("/", recursive=False)  # Returns: ["/file1.txt"]

            # List files recursively with details
            fs.list(details=True)  # Returns: [{"path": "/file1.txt", "size": 100, ...}, ...]

            # Old API (deprecated but supported)
            fs.list(prefix="/dir")  # Returns all files under /dir recursively
        """
        # Handle backward compatibility with old 'prefix' parameter
        if prefix is not None:
            # Old API: list(prefix="/path") - always recursive
            if prefix:
                prefix = self._validate_path(prefix)
            all_files = self.metadata.list(prefix)
            results = all_files
        else:
            # New API: list(path="/", recursive=False)
            if path:
                path = self._validate_path(path)

            # Ensure path ends with / for directory listing
            if not path.endswith("/"):
                path = path + "/"

            # Get all files with this prefix
            all_files = self.metadata.list(path if path != "/" else "")

            if recursive:
                # Include all files under this path
                results = all_files
            else:
                # Only include files directly in this directory (no subdirectories)
                results = []
                for meta in all_files:
                    # Remove the prefix to get relative path
                    rel_path = meta.path[len(path) :] if path != "/" else meta.path[1:]
                    # If there's no "/" in the relative path, it's in this directory
                    if "/" not in rel_path:
                        results.append(meta)

        # Sort by path name
        results.sort(key=lambda m: m.path)

        if details:
            return [
                {
                    "path": meta.path,
                    "size": meta.size,
                    "modified_at": meta.modified_at,
                    "created_at": meta.created_at,
                    "etag": meta.etag,
                    "mime_type": meta.mime_type,
                }
                for meta in results
            ]
        else:
            return [meta.path for meta in results]

    def glob(self, pattern: str, path: str = "/") -> builtins.list[str]:
        """
        Find files matching a glob pattern.

        Supports standard glob patterns:
        - `*` matches any sequence of characters (except `/`)
        - `**` matches any sequence of characters including `/` (recursive)
        - `?` matches any single character
        - `[...]` matches any character in the brackets

        Args:
            pattern: Glob pattern to match (e.g., "**/*.py", "data/*.csv", "test_*.py")
            path: Base path to search from (default: "/")

        Returns:
            List of matching file paths, sorted by name

        Examples:
            # Find all Python files recursively
            fs.glob("**/*.py")  # Returns: ["/src/main.py", "/tests/test_foo.py", ...]

            # Find all CSV files in data directory
            fs.glob("*.csv", "/data")  # Returns: ["/data/file1.csv", "/data/file2.csv"]

            # Find all test files
            fs.glob("test_*.py")  # Returns: ["/test_foo.py", "/test_bar.py"]
        """
        if path:
            path = self._validate_path(path)

        # Get all files
        all_files = self.metadata.list("")

        # Build full pattern
        if not path.endswith("/"):
            path = path + "/"
        if path == "/":
            full_pattern = pattern
        else:
            # Remove leading / from path for pattern matching
            base_path = path[1:] if path.startswith("/") else path
            full_pattern = base_path + pattern

        # Match files against pattern
        # Handle ** for recursive matching
        if "**" in full_pattern:
            # Convert glob pattern to regex
            # Split by ** to handle recursive matching
            parts = full_pattern.split("**")

            regex_parts = []
            for i, part in enumerate(parts):
                if i > 0:
                    # ** matches zero or more path segments
                    # This can be empty or ".../", so use (?:.*/)? for optional match
                    regex_parts.append("(?:.*/)?")

                # Escape and convert wildcards in this part
                escaped = re.escape(part)
                escaped = escaped.replace(r"\*", "[^/]*")
                escaped = escaped.replace(r"\?", ".")
                escaped = escaped.replace(r"\[", "[").replace(r"\]", "]")

                # Remove leading / from all parts since it's handled by ** or the anchor
                # Note: re.escape() doesn't escape /, so we check for it directly
                while escaped.startswith("/"):
                    escaped = escaped[1:]

                regex_parts.append(escaped)

            regex_pattern = "^/" + "".join(regex_parts) + "$"

            matches = []
            for meta in all_files:
                if re.match(regex_pattern, meta.path):
                    matches.append(meta.path)
        else:
            # Use fnmatch for simpler patterns
            matches = []
            for meta in all_files:
                # Remove leading / for matching
                file_path = meta.path[1:] if meta.path.startswith("/") else meta.path
                if fnmatch.fnmatch(file_path, full_pattern):
                    matches.append(meta.path)

        return sorted(matches)

    def grep(
        self,
        pattern: str,
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 1000,
    ) -> builtins.list[dict[str, Any]]:
        r"""
        Search file contents using regex patterns.

        Args:
            pattern: Regex pattern to search for in file contents
            path: Base path to search from (default: "/")
            file_pattern: Optional glob pattern to filter files (e.g., "*.py")
            ignore_case: If True, perform case-insensitive search (default: False)
            max_results: Maximum number of results to return (default: 1000)

        Returns:
            List of match dicts, each containing:
            - file: File path
            - line: Line number (1-indexed)
            - content: Matched line content
            - match: The matched text

        Examples:
            # Search for "TODO" in all files
            fs.grep("TODO")  # Returns: [{"file": "/main.py", "line": 42, "content": "# TODO: ...", ...}, ...]

            # Search for function definitions in Python files
            fs.grep(r"def \w+", file_pattern="**/*.py")

            # Case-insensitive search
            fs.grep("error", ignore_case=True)
        """
        if path:
            path = self._validate_path(path)

        # Compile regex pattern
        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern: {e}") from e

        # Get files to search
        files: list[str]
        if file_pattern:
            files = self.glob(file_pattern, path)
        else:
            # Get all files under path
            if not path.endswith("/"):
                path = path + "/"
            prefix = path if path != "/" else ""
            all_files = self.metadata.list(prefix)
            files = [meta.path for meta in all_files]

        # Search through files
        results: list[dict[str, Any]] = []
        for file_path in files:
            if len(results) >= max_results:
                break

            try:
                # Read file content
                content = self.read(file_path)

                # Try to decode as text
                try:
                    text = content.decode("utf-8")
                except UnicodeDecodeError:
                    # Skip binary files
                    continue

                # Search line by line
                for line_num, line in enumerate(text.splitlines(), start=1):
                    if len(results) >= max_results:
                        break

                    match = regex.search(line)
                    if match:
                        results.append(
                            {
                                "file": file_path,
                                "line": line_num,
                                "content": line,
                                "match": match.group(0),
                            }
                        )

            except Exception:
                # Skip files that can't be read
                continue

        return results

    # === Directory Operations ===

    def mkdir(self, path: str, parents: bool = False, exist_ok: bool = False) -> None:
        """
        Create a directory.

        Args:
            path: Virtual path to directory
            parents: Create parent directories if needed (like mkdir -p)
            exist_ok: Don't raise error if directory exists

        Raises:
            FileExistsError: If directory exists and exist_ok=False
            FileNotFoundError: If parent doesn't exist and parents=False
            InvalidPathError: If path is invalid
            BackendError: If operation fails
            AccessDeniedError: If access is denied (tenant isolation or read-only namespace)
            PermissionError: If path is read-only
        """
        path = self._validate_path(path)

        # Route to backend with write access check (mkdir requires write permission)
        route = self.router.route(
            path,
            tenant_id=self.tenant_id,
            agent_id=self.agent_id,
            is_admin=self.is_admin,
            check_write=True,
        )

        # Check if path is read-only
        if route.readonly:
            raise PermissionError(f"Cannot create directory in read-only path: {path}")

        # Create directory in backend
        route.backend.mkdir(route.backend_path, parents=parents, exist_ok=exist_ok)

    def rmdir(self, path: str, recursive: bool = False) -> None:
        """
        Remove a directory.

        Args:
            path: Virtual path to directory
            recursive: Remove non-empty directory (like rm -rf)

        Raises:
            OSError: If directory not empty and recursive=False
            NexusFileNotFoundError: If directory doesn't exist
            InvalidPathError: If path is invalid
            BackendError: If operation fails
            AccessDeniedError: If access is denied (tenant isolation or read-only namespace)
            PermissionError: If path is read-only
        """
        import errno

        path = self._validate_path(path)

        # Route to backend with write access check (rmdir requires write permission)
        route = self.router.route(
            path,
            tenant_id=self.tenant_id,
            agent_id=self.agent_id,
            is_admin=self.is_admin,
            check_write=True,
        )

        # Check readonly
        if route.readonly:
            raise PermissionError(f"Cannot remove directory from read-only path: {path}")

        # Check if directory contains any files in metadata store
        # Normalize path to ensure it ends with /
        dir_path = path if path.endswith("/") else path + "/"
        files_in_dir = self.metadata.list(dir_path)

        if files_in_dir:
            # Directory is not empty
            if not recursive:
                # Raise OSError with ENOTEMPTY errno (same as os.rmdir behavior)
                raise OSError(errno.ENOTEMPTY, f"Directory not empty: {path}")

            # Recursive mode - delete all files in directory
            # Use batch delete for better performance (single transaction instead of N queries)
            file_paths = [file_meta.path for file_meta in files_in_dir]

            # Delete content from backend for each file
            for file_meta in files_in_dir:
                if file_meta.etag:
                    with contextlib.suppress(Exception):
                        route.backend.delete_content(file_meta.etag)

            # Batch delete from metadata store
            self.metadata.delete_batch(file_paths)

        # Remove directory in backend (if it still exists)
        # In CAS systems, the directory may no longer exist after deleting its contents
        with contextlib.suppress(NexusFileNotFoundError):
            route.backend.rmdir(route.backend_path, recursive=recursive)

    def is_directory(self, path: str) -> bool:
        """
        Check if path is a directory.

        Args:
            path: Virtual path to check

        Returns:
            True if path is a directory, False otherwise
        """
        try:
            path = self._validate_path(path)
            # Route with access control (read permission needed to check)
            route = self.router.route(
                path,
                tenant_id=self.tenant_id,
                agent_id=self.agent_id,
                is_admin=self.is_admin,
                check_write=False,
            )
            return route.backend.is_directory(route.backend_path)
        except (InvalidPathError, Exception):
            return False

    # === Metadata Export/Import ===

    def export_metadata(self, output_path: str | Path, prefix: str = "") -> int:
        """
        Export metadata to JSONL file for backup and migration.

        Each line in the output file is a JSON object containing:
        - path: Virtual file path
        - backend_name: Backend identifier
        - physical_path: Physical storage path (content hash in CAS)
        - size: File size in bytes
        - etag: Content hash (SHA-256)
        - mime_type: MIME type (optional)
        - created_at: Creation timestamp (ISO format)
        - modified_at: Modification timestamp (ISO format)
        - version: Version number
        - custom_metadata: Dict of custom key-value metadata (optional)

        Args:
            output_path: Path to output JSONL file
            prefix: Optional path prefix to filter exported files (default: "" exports all)

        Returns:
            Number of files exported

        Examples:
            # Export all metadata
            count = fs.export_metadata("backup.jsonl")

            # Export only /workspace files
            count = fs.export_metadata("workspace-backup.jsonl", prefix="/workspace")
        """
        import json

        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Get all files matching prefix
        all_files = self.metadata.list(prefix)
        count = 0

        with output_file.open("w", encoding="utf-8") as f:
            for file_meta in all_files:
                # Build base metadata dict
                metadata_dict: dict[str, Any] = {
                    "path": file_meta.path,
                    "backend_name": file_meta.backend_name,
                    "physical_path": file_meta.physical_path,
                    "size": file_meta.size,
                    "etag": file_meta.etag,
                    "mime_type": file_meta.mime_type,
                    "created_at": (
                        file_meta.created_at.isoformat() if file_meta.created_at else None
                    ),
                    "modified_at": (
                        file_meta.modified_at.isoformat() if file_meta.modified_at else None
                    ),
                    "version": file_meta.version,
                }

                # Try to get custom metadata for this file (if any)
                # Note: This is optional - files may not have custom metadata
                try:
                    if isinstance(self.metadata, SQLAlchemyMetadataStore):
                        # Get all custom metadata keys for this path
                        # We need to query the database directly for all keys
                        with self.metadata.SessionLocal() as session:
                            from nexus.storage.models import FileMetadataModel, FilePathModel

                            # Get path_id
                            path_stmt = select(FilePathModel.path_id).where(
                                FilePathModel.virtual_path == file_meta.path,
                                FilePathModel.deleted_at.is_(None),
                            )
                            path_id = session.scalar(path_stmt)

                            if path_id:
                                # Get all custom metadata
                                meta_stmt = select(FileMetadataModel).where(
                                    FileMetadataModel.path_id == path_id
                                )
                                custom_meta = {}
                                for meta_item in session.scalars(meta_stmt):
                                    if meta_item.value:
                                        custom_meta[meta_item.key] = json.loads(meta_item.value)

                                if custom_meta:
                                    metadata_dict["custom_metadata"] = custom_meta
                except Exception:
                    # Ignore errors when fetching custom metadata
                    pass

                # Write JSON line
                f.write(json.dumps(metadata_dict) + "\n")
                count += 1

        return count

    def import_metadata(
        self,
        input_path: str | Path,
        overwrite: bool = False,
        skip_existing: bool = True,
    ) -> tuple[int, int]:
        """
        Import metadata from JSONL file.

        IMPORTANT: This only imports metadata records, not the actual file content.
        The content must already exist in the CAS storage (matched by content hash).
        This is useful for:
        - Restoring metadata after database corruption
        - Migrating metadata between instances (with same CAS content)
        - Creating alternative path mappings to existing content

        Args:
            input_path: Path to input JSONL file
            overwrite: If True, overwrite existing metadata (default: False)
            skip_existing: If True, skip files that already exist (default: True)

        Returns:
            Tuple of (imported_count, skipped_count)

        Raises:
            ValueError: If JSONL format is invalid
            FileNotFoundError: If input file doesn't exist

        Examples:
            # Import metadata (skip existing)
            imported, skipped = fs.import_metadata("backup.jsonl")
            print(f"Imported {imported} files, skipped {skipped}")

            # Import and overwrite existing
            imported, skipped = fs.import_metadata("backup.jsonl", overwrite=True)
        """
        import json

        input_file = Path(input_path)
        if not input_file.exists():
            raise FileNotFoundError(f"Input file not found: {input_path}")

        imported_count = 0
        skipped_count = 0

        with input_file.open("r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue

                try:
                    # Parse JSON line
                    metadata_dict = json.loads(line)

                    # Validate required fields
                    required_fields = ["path", "backend_name", "physical_path", "size"]
                    for field in required_fields:
                        if field not in metadata_dict:
                            raise ValueError(f"Missing required field: {field}")

                    path = metadata_dict["path"]

                    # Check if file already exists
                    existing = self.metadata.get(path)
                    if existing:
                        if skip_existing and not overwrite:
                            skipped_count += 1
                            continue
                        if not overwrite:
                            skipped_count += 1
                            continue

                    # Parse timestamps
                    created_at = None
                    if metadata_dict.get("created_at"):
                        created_at = datetime.fromisoformat(metadata_dict["created_at"])

                    modified_at = None
                    if metadata_dict.get("modified_at"):
                        modified_at = datetime.fromisoformat(metadata_dict["modified_at"])

                    # Create FileMetadata object
                    file_meta = FileMetadata(
                        path=path,
                        backend_name=metadata_dict["backend_name"],
                        physical_path=metadata_dict["physical_path"],
                        size=metadata_dict["size"],
                        etag=metadata_dict.get("etag"),
                        mime_type=metadata_dict.get("mime_type"),
                        created_at=created_at,
                        modified_at=modified_at,
                        version=metadata_dict.get("version", 1),
                    )

                    # Store metadata
                    self.metadata.put(file_meta)

                    # Import custom metadata if present
                    if "custom_metadata" in metadata_dict:
                        custom_meta = metadata_dict["custom_metadata"]
                        if isinstance(custom_meta, dict):
                            for key, value in custom_meta.items():
                                with contextlib.suppress(Exception):
                                    # Ignore errors when setting custom metadata
                                    self.metadata.set_file_metadata(path, key, value)

                    imported_count += 1

                except json.JSONDecodeError as e:
                    raise ValueError(f"Invalid JSON at line {line_num}: {e}") from e
                except Exception as e:
                    raise ValueError(f"Error processing line {line_num}: {e}") from e

        return imported_count, skipped_count

    def close(self) -> None:
        """Close the embedded filesystem and release resources."""
        self.metadata.close()
