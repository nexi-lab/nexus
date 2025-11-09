"""Core file operations for NexusFS.

This module contains the fundamental file operations:
- read: Read file content
- write: Write file content with optimistic concurrency control
- delete: Delete files
- rename: Rename/move files
- exists: Check file existence
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.core.exceptions import ConflictError, NexusFileNotFoundError
from nexus.core.metadata import FileMetadata
from nexus.core.permissions import Permission
from nexus.core.rpc_decorator import rpc_expose

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.backends.backend import Backend
    from nexus.core.permission_policy import PolicyMatcher
    from nexus.core.permissions import OperationContext
    from nexus.core.router import PathRouter
    from nexus.parsers.registry import ParserRegistry
    from nexus.storage.metadata_store import SQLAlchemyMetadataStore


class NexusFSCoreMixin:
    """Mixin providing core file operations for NexusFS."""

    # Type hints for attributes/methods that will be provided by NexusFS parent class
    if TYPE_CHECKING:
        metadata: SQLAlchemyMetadataStore
        backend: Backend
        router: PathRouter
        is_admin: bool
        auto_parse: bool
        parser_registry: ParserRegistry
        policy_matcher: PolicyMatcher
        _default_context: OperationContext
        _parser_threads: list[threading.Thread]
        _parser_threads_lock: threading.Lock

        @property
        def tenant_id(self) -> str | None: ...
        @property
        def agent_id(self) -> str | None: ...

        def _validate_path(self, path: str) -> str: ...
        def _check_permission(
            self, path: str, permission: Permission, context: OperationContext | None
        ) -> None: ...
        def _inherit_permissions_from_parent(
            self, path: str, is_directory: bool
        ) -> tuple[str | None, str | None, int | None]: ...
        def _get_routing_params(
            self, context: OperationContext | dict[Any, Any] | None
        ) -> tuple[str | None, str | None, bool]: ...
        def _get_created_by(
            self, context: OperationContext | dict[Any, Any] | None
        ) -> str | None: ...
        async def parse(self, path: str, store_result: bool = True) -> Any: ...

    def _apply_dynamic_viewer_filter_if_needed(
        self, path: str, content: bytes, context: OperationContext | None
    ) -> bytes:
        """Apply dynamic_viewer column-level filtering for CSV files if needed.

        Args:
            path: File path
            content: Original file content
            context: Operation context

        Returns:
            Filtered content if dynamic_viewer permission exists, otherwise original content
        """
        # Only process CSV files
        if not path.lower().endswith(".csv"):
            logger.debug(f"_apply_dynamic_viewer_filter: Skipping non-CSV file: {path}")
            return content

        # Extract subject from context (uses NexusFSReBACMixin method)
        if not hasattr(self, "_get_subject_from_context"):
            logger.debug("_apply_dynamic_viewer_filter: No _get_subject_from_context method")
            return content

        subject = self._get_subject_from_context(context)
        if not subject:
            logger.debug(f"_apply_dynamic_viewer_filter: No subject found in context for {path}")
            return content

        logger.debug(
            f"_apply_dynamic_viewer_filter: Checking dynamic_viewer for {subject} on {path}"
        )

        # Check if ReBAC is available
        if not hasattr(self, "_rebac_manager") or not hasattr(self, "get_dynamic_viewer_config"):
            logger.debug(
                "_apply_dynamic_viewer_filter: ReBAC or get_dynamic_viewer_config not available"
            )
            return content

        try:
            # Get dynamic_viewer configuration for this subject + file
            column_config = self.get_dynamic_viewer_config(subject=subject, file_path=path)  # type: ignore[attr-defined]

            if not column_config:
                # No dynamic_viewer permission, return original content
                logger.debug(
                    f"_apply_dynamic_viewer_filter: No dynamic_viewer config for {subject} on {path}"
                )
                return content

            logger.info(
                f"_apply_dynamic_viewer_filter: Applying filter for {subject} on {path}: {column_config}"
            )

            # Apply filtering
            content_str = content.decode("utf-8") if isinstance(content, bytes) else content
            result = self.apply_dynamic_viewer_filter(  # type: ignore[attr-defined]
                data=content_str, column_config=column_config, file_format="csv"
            )

            # Return filtered content as bytes
            filtered_content = result["filtered_data"]
            logger.info(f"_apply_dynamic_viewer_filter: Successfully filtered {path}")
            if isinstance(filtered_content, str):
                return filtered_content.encode("utf-8")
            elif isinstance(filtered_content, bytes):
                return filtered_content
            else:
                # Fallback: convert to string then bytes
                return str(filtered_content).encode("utf-8")

        except Exception as e:
            # Log error but don't fail the read operation
            logger.warning(f"Failed to apply dynamic_viewer filter for {path}: {e}")
            import traceback

            logger.warning(traceback.format_exc())
            return content

    @rpc_expose(description="Read file content")
    def read(
        self, path: str, context: OperationContext | None = None, return_metadata: bool = False
    ) -> bytes | dict[str, Any]:
        """
        Read file content as bytes.

        Args:
            path: Virtual path to read (supports memory virtual paths)
            context: Optional operation context for permission checks (uses default if not provided)
            return_metadata: If True, return dict with content and metadata (etag, version, modified_at).
                           If False, return only content bytes (default: False)

        Returns:
            If return_metadata=False: File content as bytes
            If return_metadata=True: Dict with keys:
                - content: File content as bytes
                - etag: Content hash (SHA-256) for optimistic concurrency
                - version: Current version number
                - modified_at: Last modification timestamp
                - size: File size in bytes

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            BackendError: If read operation fails
            AccessDeniedError: If access is denied based on tenant isolation
            PermissionError: If user doesn't have read permission

        Examples:
            >>> # Read content only
            >>> content = nx.read("/workspace/data.json")
            >>> print(content)
            b'{"key": "value"}'

            >>> # Read with metadata for optimistic concurrency
            >>> result = nx.read("/workspace/data.json", return_metadata=True)
            >>> content = result['content']
            >>> etag = result['etag']
            >>> # Later, write with version check
            >>> nx.write("/workspace/data.json", new_content, if_match=etag)

            >>> # Read memory via virtual path            >>> content = nx.read("/workspace/alice/agent1/memory/facts")
            >>> content = nx.read("/memory/by-user/alice/facts")  # Same memory!
        """
        path = self._validate_path(path)

        # Phase 2 Integration: Intercept memory paths
        from nexus.core.memory_router import MemoryViewRouter

        if MemoryViewRouter.is_memory_path(path):
            return self._read_memory_path(path, return_metadata, context=context)

        # Check read permission (handles virtual views by checking original file)
        self._check_permission(path, Permission.READ, context)

        # Fix #332: Handle virtual parsed views (e.g., report_parsed.pdf.md)
        from nexus.core.virtual_views import get_parsed_content, parse_virtual_path

        def metadata_exists(check_path: str) -> bool:
            return self.metadata.exists(check_path)

        original_path, view_type = parse_virtual_path(path, metadata_exists)
        if view_type == "md":
            # This is a virtual view - read and parse the original file
            logger.info(f"read: Virtual view detected, reading original file: {original_path}")

            # Read the original file
            tenant_id, agent_id, is_admin = self._get_routing_params(context)
            route = self.router.route(
                original_path,
                tenant_id=tenant_id,
                agent_id=agent_id,
                is_admin=is_admin,
                check_write=False,
            )
            meta = self.metadata.get(original_path)
            if meta is None or meta.etag is None:
                raise NexusFileNotFoundError(original_path)

            original_content = route.backend.read_content(meta.etag, context=context)

            # Apply dynamic_viewer filtering for CSV files before parsing
            original_content = self._apply_dynamic_viewer_filter_if_needed(
                original_path, original_content, context
            )

            # Parse the content
            content = get_parsed_content(original_content, original_path, view_type)

            # Return parsed content with simulated metadata
            if return_metadata:
                return {
                    "content": content,
                    "etag": meta.etag + ".md",  # Synthetic etag for virtual view
                    "version": meta.version,
                    "modified_at": meta.modified_at,
                    "size": len(content),
                }
            return content

        # Normal file path - proceed with regular read
        tenant_id, agent_id, is_admin = self._get_routing_params(context)
        route = self.router.route(
            path,
            tenant_id=tenant_id,
            agent_id=agent_id,
            is_admin=is_admin,
            check_write=False,
        )

        # Check if file exists in metadata
        meta = self.metadata.get(path)
        if meta is None or meta.etag is None:
            raise NexusFileNotFoundError(path)

        # Read from routed backend using content hash
        content = route.backend.read_content(meta.etag, context=context)

        # Apply dynamic_viewer filtering for CSV files
        content = self._apply_dynamic_viewer_filter_if_needed(path, content, context)

        # Return content with metadata if requested
        if return_metadata:
            # Update size after filtering
            return {
                "content": content,
                "etag": meta.etag,
                "version": meta.version,
                "modified_at": meta.modified_at,
                "size": len(content),  # Update size after filtering
            }

        return content

    @rpc_expose(description="Stream file content in chunks")
    def stream(
        self, path: str, chunk_size: int = 8192, context: OperationContext | None = None
    ) -> Any:
        """
        Stream file content in chunks without loading entire file into memory.

        This is a memory-efficient alternative to read() for large files.
        Yields chunks as an iterator, allowing processing of files larger than RAM.

        Args:
            path: Virtual path to stream
            chunk_size: Size of each chunk in bytes (default: 8KB)
            context: Optional operation context for permission checks

        Yields:
            bytes: Chunks of file content

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            BackendError: If stream operation fails
            AccessDeniedError: If access is denied
            PermissionError: If user doesn't have read permission

        Example:
            >>> # Stream large file efficiently
            >>> for chunk in nx.stream("/workspace/large_file.bin"):
            ...     process(chunk)  # Memory usage = chunk_size, not file_size

            >>> # Stream to output
            >>> import sys
            >>> for chunk in nx.stream("/workspace/video.mp4", chunk_size=1024*1024):  # 1MB chunks
            ...     sys.stdout.buffer.write(chunk)
        """
        path = self._validate_path(path)

        # Check read permission
        self._check_permission(path, Permission.READ, context)

        # Route to backend with access control
        tenant_id, agent_id, is_admin = self._get_routing_params(context)
        route = self.router.route(
            path,
            tenant_id=tenant_id,
            agent_id=agent_id,
            is_admin=is_admin,
            check_write=False,
        )

        # Check if file exists in metadata
        meta = self.metadata.get(path)
        if meta is None or meta.etag is None:
            raise NexusFileNotFoundError(path)

        # Stream from routed backend using content hash
        yield from route.backend.stream_content(meta.etag, chunk_size=chunk_size, context=context)

    @rpc_expose(description="Write file content")
    def write(
        self,
        path: str,
        content: bytes | str,
        context: OperationContext | None = None,
        if_match: str | None = None,
        if_none_match: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """
        Write content to a file with optional optimistic concurrency control.

        Creates parent directories if needed. Overwrites existing files.
        Updates metadata store.

        Automatically deduplicates content using CAS.

        Args:
            path: Virtual path to write
            content: File content as bytes or str (str will be UTF-8 encoded)
            context: Optional operation context for permission checks (uses default if not provided)
            if_match: Optional etag for optimistic concurrency control (v0.3.9).
                     If provided, write only succeeds if current file etag matches this value.
                     Prevents concurrent modification conflicts.
            if_none_match: If True, write only if file doesn't exist (create-only mode)
            force: If True, skip version check and overwrite unconditionally (dangerous!)

        Returns:
            Dict with metadata about the written file:
                - etag: Content hash (SHA-256) of the written content
                - version: New version number
                - modified_at: Modification timestamp
                - size: File size in bytes

        Raises:
            InvalidPathError: If path is invalid
            BackendError: If write operation fails
            AccessDeniedError: If access is denied (tenant isolation or read-only namespace)
            PermissionError: If path is read-only or user doesn't have write permission
            ConflictError: If if_match is provided and doesn't match current etag            FileExistsError: If if_none_match=True and file already exists

        Examples:
            >>> # Simple write (no version checking)
            >>> result = nx.write("/workspace/data.json", b'{"key": "value"}')
            >>> print(result['etag'], result['version'])

            >>> # Optimistic concurrency control
            >>> result = nx.read("/workspace/data.json", return_metadata=True)
            >>> new_content = modify(result['content'])
            >>> try:
            ...     nx.write("/workspace/data.json", new_content, if_match=result['etag'])
            ... except ConflictError:
            ...     print("File was modified by another agent!")

            >>> # Create-only mode
            >>> nx.write("/workspace/new.txt", b'content', if_none_match=True)

            >>> # Write memory via virtual path            >>> nx.write("/workspace/alice/agent1/memory/facts", b'Python is great')
            >>> nx.write("/memory/by-user/alice/facts", b'Update')  # Same memory!
        """
        # Auto-convert str to bytes for convenience
        if isinstance(content, str):
            content = content.encode("utf-8")

        path = self._validate_path(path)

        # Phase 2 Integration: Intercept memory paths
        from nexus.core.memory_router import MemoryViewRouter

        if MemoryViewRouter.is_memory_path(path):
            return self._write_memory_path(path, content)

        # Route to backend with write access check FIRST (to check tenant/agent isolation)
        # This must happen before permission check so AccessDeniedError is raised before PermissionError
        tenant_id, agent_id, is_admin = self._get_routing_params(context)
        route = self.router.route(
            path,
            tenant_id=tenant_id,
            agent_id=agent_id,
            is_admin=is_admin,
            check_write=True,
        )

        # Check if path is read-only
        if route.readonly:
            raise PermissionError(f"Path is read-only: {path}")

        # Get existing metadata for permission check and update detection (single query)
        now = datetime.now(UTC)
        meta = self.metadata.get(path)

        # Capture snapshot before operation for undo capability
        snapshot_hash = meta.etag if meta else None
        metadata_snapshot = None
        if meta:
            metadata_snapshot = {
                "size": meta.size,
                "version": meta.version,
                "modified_at": meta.modified_at.isoformat() if meta.modified_at else None,
            }

        # Check write permission (use ReBAC, not UNIX permissions)
        if self._enforce_permissions:  # type: ignore[attr-defined]
            import logging

            logger = logging.getLogger(__name__)

            ctx = context or self._default_context
            logger.info(
                f"📝 WRITE PERMISSION CHECK: path={path}, meta_exists={meta is not None}, user={ctx.user}, is_admin={ctx.is_admin}"
            )

            if meta is not None:
                # For existing files, check permission on the file itself
                logger.info(f"  -> ⚠️  File metadata EXISTS - checking permission on FILE: {path}")
                logger.info(
                    f"  -> Existing file etag: {meta.etag}, version: {meta.version}, size: {meta.size}"
                )
                self._check_permission(path, Permission.WRITE, ctx)
            else:
                # For new files, check permission on parent directory
                parent_path = self._get_parent_path(path)  # type: ignore[attr-defined]
                logger.info(f"  -> ✨ NEW file - checking permission on PARENT: {parent_path}")
                if parent_path:
                    self._check_permission(parent_path, Permission.WRITE, ctx)

        # Optimistic concurrency control
        if not force:
            # Check if_none_match (create-only mode)
            if if_none_match and meta is not None:
                raise FileExistsError(f"File already exists: {path}")

            # Check if_match (version check)
            if if_match is not None:
                if meta is None:
                    # File doesn't exist, can't match etag
                    raise ConflictError(
                        path=path,
                        expected_etag=if_match,
                        current_etag="(file does not exist)",
                    )
                elif meta.etag != if_match:
                    # Version mismatch - conflict detected!
                    raise ConflictError(
                        path=path,
                        expected_etag=if_match,
                        current_etag=meta.etag or "(no etag)",
                    )

        # Write to routed backend - returns content hash
        content_hash = route.backend.write_content(content, context=context)

        # NOTE: Do NOT delete old content when updating a file!
        # Version history preserves references to old content hashes.
        # Old content should only be deleted when ALL versions are deleted.
        # CAS reference counting handles cleanup automatically.

        # UNIX permissions removed - all access control via ReBAC

        # Calculate new version number (increment if updating)
        new_version = (meta.version + 1) if meta else 1

        # Store metadata with content hash as both etag and physical_path
        # Note: UNIX permissions (owner/group/mode) removed - use ReBAC instead
        metadata = FileMetadata(
            path=path,
            backend_name=self.backend.name,
            physical_path=content_hash,  # CAS: hash is the "physical" location
            size=len(content),
            etag=content_hash,  # SHA-256 hash for integrity
            created_at=meta.created_at if meta else now,
            modified_at=now,
            version=new_version,
            created_by=self._get_created_by(context),  # Track who created/modified this version
        )

        self.metadata.put(metadata)

        # P0-3: Create parent relationship tuples for file inheritance
        # This enables permission inheritance from parent directories
        import logging

        logger = logging.getLogger(__name__)

        if hasattr(self, "_hierarchy_manager"):
            try:
                ctx = context if context is not None else self._default_context
                logger.info(
                    f"write: Calling ensure_parent_tuples for {path}, tenant_id={ctx.tenant_id or 'default'}"
                )
                created_count = self._hierarchy_manager.ensure_parent_tuples(
                    path, tenant_id=ctx.tenant_id or "default"
                )
                logger.info(f"write: Created {created_count} parent tuples for {path}")
            except Exception as e:
                # Log the error but don't fail the write operation
                logger.warning(
                    f"write: Failed to create parent tuples for {path}: {type(e).__name__}: {e}"
                )

        # Auto-parse file if enabled and format is supported
        if self.auto_parse:
            self._auto_parse_file(path)

        # Log operation for audit trail and undo capability        # P0 COMPLIANCE FIX: Properly handle audit log failures instead of silently ignoring them
        try:
            from nexus.storage.operation_logger import OperationLogger

            with self.metadata.SessionLocal() as session:
                op_logger = OperationLogger(session)
                op_logger.log_operation(
                    operation_type="write",
                    path=path,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    snapshot_hash=snapshot_hash,
                    metadata_snapshot=metadata_snapshot,
                    status="success",
                )
                session.commit()
        except Exception as e:
            # P0 COMPLIANCE FIX: Handle audit log failures based on audit_strict_mode
            import logging

            from nexus.core.exceptions import AuditLogError

            logger = logging.getLogger(__name__)

            if self._audit_strict_mode:  # type: ignore[attr-defined]
                # STRICT MODE (default): Fail the write operation to ensure audit trail completeness
                # This is required for compliance with SOX, HIPAA, GDPR, PCI DSS
                logger.error(
                    f"AUDIT LOG FAILURE: Write to '{path}' ABORTED due to audit logging failure. "
                    f"Error: {e}. Enable audit_strict_mode=False to allow writes without audit logs."
                )
                raise AuditLogError(
                    f"Write operation aborted: audit logging failed: {e}",
                    path=path,
                    original_error=e,
                ) from e
            else:
                # PERMISSIVE MODE: Allow write to succeed but log at CRITICAL level
                # WARNING: This creates audit trail gaps and may violate compliance requirements
                logger.critical(
                    f"AUDIT LOG FAILURE: Write to '{path}' SUCCEEDED but audit log FAILED. "
                    f"Error: {e}. This creates an audit trail gap! "
                    f"Enable audit_strict_mode=True to prevent this."
                )
                # Continue execution - write succeeded but audit log failed

        # v0.7.0: Fire workflow event for automatic trigger execution
        if self.enable_workflows and self.workflow_engine:  # type: ignore[attr-defined]
            import asyncio

            from nexus.workflows.types import TriggerType

            # Determine if this is a new file or update
            is_new_file = meta is None or meta.etag is None

            event_context = {
                "file_path": path,
                "size": len(content),
                "etag": content_hash,
                "version": new_version,
                "tenant_id": tenant_id or "default",
                "agent_id": agent_id,
                "user_id": context.user_id if context and hasattr(context, "user_id") else None,
                "created": is_new_file,
                "timestamp": now.isoformat(),
            }

            # Fire event asynchronously (don't block file write)
            import sys

            print(
                f"[DEBUG] About to fire workflow for: {event_context.get('file_path')}",
                file=sys.stderr,
            )
            print(
                f"[DEBUG] Has workflow_engine: {hasattr(self, 'workflow_engine')}", file=sys.stderr
            )

            try:
                # Try to get the running event loop and create task
                asyncio.get_running_loop()
                print("[DEBUG] Event loop found, creating task", file=sys.stderr)
                asyncio.create_task(
                    self.workflow_engine.fire_event(  # type: ignore[attr-defined]
                        TriggerType.FILE_WRITE, event_context
                    )
                )
            except RuntimeError as e:
                print(f"[DEBUG] No event loop, using thread. Error was: {e}", file=sys.stderr)
                # No event loop running - run workflow synchronously in background thread
                # This happens in synchronous contexts (like DeepAgents)
                import logging
                import threading

                logger = logging.getLogger(__name__)

                def run_workflow() -> None:
                    try:
                        print(
                            f"[WORKFLOW THREAD] Starting for {event_context.get('file_path')}",
                            file=sys.stderr,
                        )
                        asyncio.run(
                            self.workflow_engine.fire_event(  # type: ignore[attr-defined]
                                TriggerType.FILE_WRITE, event_context
                            )
                        )
                        print("[WORKFLOW THREAD] Completed successfully", file=sys.stderr)
                    except Exception as e:
                        print(f"[WORKFLOW THREAD] Error: {e}", file=sys.stderr)
                        import traceback

                        traceback.print_exc(file=sys.stderr)

                thread = threading.Thread(target=run_workflow, daemon=True)
                thread.start()
                logger.info(
                    f"[WORKFLOW] Started background thread for {event_context.get('file_path')}"
                )

        # Return metadata for optimistic concurrency control
        return {
            "etag": content_hash,
            "version": new_version,
            "modified_at": now,
            "size": len(content),
        }

    @rpc_expose(description="Append content to an existing file or create if it doesn't exist")
    def append(
        self,
        path: str,
        content: bytes | str,
        context: OperationContext | None = None,
        if_match: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """
        Append content to an existing file or create a new file if it doesn't exist.

        This is an efficient way to add content to files without reading the entire
        file separately, particularly useful for:
        - Writing JSONL (JSON Lines) logs incrementally
        - Appending to log files
        - Building append-only data structures
        - Streaming data collection

        Args:
            path: Virtual path to append to
            content: Content to append as bytes or str (str will be UTF-8 encoded)
            context: Optional operation context for permission checks (uses default if not provided)
            if_match: Optional etag for optimistic concurrency control.
                     If provided, append only succeeds if current file etag matches this value.
                     Prevents concurrent modification conflicts.
            force: If True, skip version check and append unconditionally (dangerous!)

        Returns:
            Dict with metadata about the written file:
                - etag: Content hash (SHA-256) of the final content (after append)
                - version: New version number
                - modified_at: Modification timestamp
                - size: Final file size in bytes

        Raises:
            InvalidPathError: If path is invalid
            BackendError: If append operation fails
            AccessDeniedError: If access is denied (tenant isolation or read-only namespace)
            PermissionError: If path is read-only or user doesn't have write permission
            ConflictError: If if_match is provided and doesn't match current etag
            NexusFileNotFoundError: If file doesn't exist during read (should not happen in normal flow)

        Examples:
            >>> # Append to a log file
            >>> nx.append("/workspace/app.log", "New log entry\\n")

            >>> # Build JSONL file incrementally
            >>> import json
            >>> for record in records:
            ...     line = json.dumps(record) + "\\n"
            ...     nx.append("/workspace/data.jsonl", line)

            >>> # Append with optimistic concurrency control
            >>> result = nx.read("/workspace/log.txt", return_metadata=True)
            >>> try:
            ...     nx.append("/workspace/log.txt", "New entry\\n", if_match=result['etag'])
            ... except ConflictError:
            ...     print("File was modified by another process!")

            >>> # Create new file if doesn't exist
            >>> nx.append("/workspace/new.txt", "First line\\n")
        """
        # Auto-convert str to bytes for convenience
        if isinstance(content, str):
            content = content.encode("utf-8")

        path = self._validate_path(path)

        # Try to read existing content if file exists
        # For non-existent files, we'll create them (existing_content stays empty)
        existing_content = b""
        try:
            result = self.read(path, context=context, return_metadata=True)
            # Type narrowing: when return_metadata=True, result is always dict
            assert isinstance(result, dict), "Expected dict when return_metadata=True"

            existing_content = result["content"]

            # If if_match is provided, verify it matches current etag
            # (the write call will also check, but we check here to fail fast)
            if if_match is not None and not force:
                current_etag = result.get("etag")
                if current_etag != if_match:
                    from nexus.core.exceptions import ConflictError

                    raise ConflictError(
                        path=path,
                        expected_etag=if_match,
                        current_etag=current_etag or "(no etag)",
                    )
        except Exception as e:
            # If file doesn't exist, treat as empty (will create new file)
            # Permission errors on non-existent files are OK - write() will check parent permissions
            from nexus.core.exceptions import NexusFileNotFoundError

            if not isinstance(e, (NexusFileNotFoundError, PermissionError)):
                # Re-raise unexpected errors
                raise
            # For FileNotFoundError or PermissionError, continue with empty content
            # write() will check if user has permission to create the file

        # Combine existing content with new content
        final_content = existing_content + content

        # Use the existing write method to handle all the complexity:
        # - Permission checking
        # - Version management
        # - Audit logging
        # - Workflow triggers
        # - Parent tuple creation
        # Note: We pass if_match to write() for additional safety
        return self.write(
            path,
            final_content,
            context=context,
            if_match=if_match,
            if_none_match=False,  # Allow both create and update
            force=force,
        )

    def write_batch(
        self, files: list[tuple[str, bytes]], context: OperationContext | None = None
    ) -> list[dict[str, Any]]:
        """
        Write multiple files in a single transaction for improved performance.

        This is 13x faster than calling write() multiple times for small files
        because it uses a single database transaction instead of N transactions.

        All files are written atomically - either all succeed or all fail.

        Args:
            files: List of (path, content) tuples to write
            context: Optional operation context for permission checks (uses default if not provided)

        Returns:
            List of metadata dicts for each file (in same order as input):
                - etag: Content hash (SHA-256) of the written content
                - version: New version number
                - modified_at: Modification timestamp
                - size: File size in bytes

        Raises:
            InvalidPathError: If any path is invalid
            BackendError: If write operation fails
            AccessDeniedError: If access is denied (tenant isolation or read-only namespace)
            PermissionError: If any path is read-only or user doesn't have write permission

        Examples:
            >>> # Write 100 small files in a single batch (13x faster!)
            >>> files = [(f"/logs/file_{i}.txt", b"log data") for i in range(100)]
            >>> results = nx.write_batch(files)
            >>> print(f"Wrote {len(results)} files")

            >>> # Atomic batch write - all or nothing
            >>> files = [
            ...     ("/config/setting1.json", b'{"enabled": true}'),
            ...     ("/config/setting2.json", b'{"timeout": 30}'),
            ... ]
            >>> nx.write_batch(files)
        """
        if not files:
            return []

        # Validate all paths first
        validated_files: list[tuple[str, bytes]] = []
        for path, content in files:
            validated_path = self._validate_path(path)
            validated_files.append((validated_path, content))

        # Route all paths and check write access
        tenant_id, agent_id, is_admin = self._get_routing_params(context)
        routes = []
        for path, _ in validated_files:
            route = self.router.route(
                path,
                tenant_id=tenant_id,
                agent_id=agent_id,
                is_admin=is_admin,
                check_write=True,
            )
            # Check if path is read-only
            if route.readonly:
                raise PermissionError(f"Path is read-only: {path}")
            routes.append(route)

        # Get existing metadata for all paths (single query)
        paths = [path for path, _ in validated_files]
        existing_metadata = self.metadata.get_batch(paths)

        # Check write permissions for existing files owned by current user
        for path in paths:
            meta = existing_metadata.get(path)
            if meta is not None and self._enforce_permissions:  # type: ignore[attr-defined]
                # Check write permissions via ReBAC
                self._check_permission(path, Permission.WRITE, context)

        now = datetime.now(UTC)
        metadata_list: list[FileMetadata] = []
        results: list[dict[str, Any]] = []

        # Write all content to backend CAS (deduplicated automatically)
        for (path, content), route in zip(validated_files, routes, strict=False):
            # Write to backend - returns content hash
            content_hash = route.backend.write_content(content, context=context)

            # Get existing metadata for this file
            meta = existing_metadata.get(path)

            # UNIX permissions removed - all access control via ReBAC

            # Calculate new version number (increment if updating)
            new_version = (meta.version + 1) if meta else 1

            # Build metadata for batch insert
            # Note: UNIX permissions (owner/group/mode) removed - use ReBAC instead
            metadata = FileMetadata(
                path=path,
                backend_name=self.backend.name,
                physical_path=content_hash,  # CAS: hash is the "physical" location
                size=len(content),
                etag=content_hash,  # SHA-256 hash for integrity
                created_at=meta.created_at if meta else now,
                modified_at=now,
                version=new_version,
                created_by=getattr(self, "agent_id", None)
                or getattr(self, "user_id", None),  # Track who created/modified this version
            )
            metadata_list.append(metadata)

            # Build result dict
            results.append(
                {
                    "etag": content_hash,
                    "version": new_version,
                    "modified_at": now,
                    "size": len(content),
                }
            )

        # Store all metadata in a single transaction (with version history)
        self.metadata.put_batch(metadata_list)

        # Auto-parse files if enabled
        if self.auto_parse:
            for path, _ in validated_files:
                self._auto_parse_file(path)

        return results

    def _auto_parse_file(self, path: str) -> None:
        """Auto-parse a file in the background (fire-and-forget).

        Args:
            path: Virtual path to the file
        """
        try:
            # Check if parser is available for this file type
            self.parser_registry.get_parser(path)

            # Run parsing in a background thread
            # CRITICAL: Use daemon=False to prevent abrupt termination during DB writes
            # Threads are tracked for graceful shutdown in close()
            thread = threading.Thread(
                target=self._parse_in_thread,
                args=(path,),
                daemon=False,  # Changed from True to prevent DB corruption on shutdown
                name=f"parser-{path}",  # Named for debugging
            )
            # Track thread for graceful shutdown
            with self._parser_threads_lock:
                # Clean up finished threads before adding new one
                self._parser_threads = [t for t in self._parser_threads if t.is_alive()]
                self._parser_threads.append(thread)
            thread.start()
        except Exception as e:
            # Log if no parser available (expected) but don't fail the write operation
            logger.debug(f"Auto-parse skipped for {path}: {type(e).__name__}: {e}")

    def _parse_in_thread(self, path: str) -> None:
        """Parse file in a background thread.

        Args:
            path: Virtual path to the file
        """
        try:
            # Run async parse in a new event loop (thread-safe)
            asyncio.run(self.parse(path, store_result=True))
        except Exception as e:
            # Log parsing errors for visibility but don't crash
            # IMPORTANT: Log with enough detail to debug issues
            import traceback

            error_type = type(e).__name__
            error_msg = str(e)

            # Categorize errors for better logging
            if "disk" in error_msg.lower() or "space" in error_msg.lower():
                logger.error(
                    f"Auto-parse FAILED for {path}: Disk error - {error_type}: {error_msg}"
                )
            elif "database" in error_msg.lower() or "connection" in error_msg.lower():
                logger.error(
                    f"Auto-parse FAILED for {path}: Database error - {error_type}: {error_msg}"
                )
            elif "memory" in error_msg.lower() or isinstance(e, MemoryError):
                logger.error(
                    f"Auto-parse FAILED for {path}: Memory error - {error_type}: {error_msg}"
                )
            elif "permission" in error_msg.lower() or isinstance(e, (PermissionError, OSError)):
                logger.warning(
                    f"Auto-parse FAILED for {path}: Permission/OS error - {error_type}: {error_msg}"
                )
            elif (
                "unsupported" in error_msg.lower()
                or "not supported" in error_msg.lower()
                or error_type == "UnsupportedFormatException"
            ):
                # Expected for files that don't need parsing - log at debug level
                logger.debug(f"Auto-parse skipped for {path}: Unsupported format - {error_msg}")
            else:
                # Unknown error - log with stack trace for debugging
                logger.warning(
                    f"Auto-parse FAILED for {path}: {error_type}: {error_msg}\n"
                    f"Stack trace:\n{traceback.format_exc()}"
                )

    @rpc_expose(description="Delete file")
    def delete(self, path: str, context: OperationContext | None = None) -> None:
        """
        Delete a file or memory.

        Removes file from backend and metadata store.
        Decrements reference count in CAS (only deletes when ref_count=0).

        Supports memory virtual paths.

        Args:
            path: Virtual path to delete (supports memory paths)
            context: Optional operation context for permission checks (uses default if not provided)

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            BackendError: If delete operation fails
            AccessDeniedError: If access is denied (tenant isolation or read-only namespace)
            PermissionError: If path is read-only or user doesn't have write permission
        """
        path = self._validate_path(path)

        # Phase 2 Integration: Intercept memory paths
        from nexus.core.memory_router import MemoryViewRouter

        if MemoryViewRouter.is_memory_path(path):
            return self._delete_memory_path(path, context=context)

        # Route to backend with write access check FIRST (to check tenant/agent isolation)
        # This must happen before permission check so AccessDeniedError is raised before PermissionError
        tenant_id, agent_id, is_admin = self._get_routing_params(context)
        route = self.router.route(
            path,
            tenant_id=tenant_id,
            agent_id=agent_id,
            is_admin=is_admin,
            check_write=True,
        )

        # Check if path is read-only
        if route.readonly:
            raise PermissionError(f"Cannot delete from read-only path: {path}")

        # Check if file exists in metadata
        meta = self.metadata.get(path)
        if meta is None:
            raise NexusFileNotFoundError(path)

        # Capture snapshot before operation for undo capability
        snapshot_hash = meta.etag
        metadata_snapshot = {
            "size": meta.size,
            "version": meta.version,
            "modified_at": meta.modified_at.isoformat() if meta.modified_at else None,
            "backend_name": meta.backend_name,
            "physical_path": meta.physical_path,
        }

        # Check write permission for delete        # This comes AFTER tenant isolation check so AccessDeniedError takes precedence
        self._check_permission(path, Permission.WRITE, context)

        # Log operation BEFORE deleting CAS content        # This ensures the snapshot is recorded while content still exists
        try:
            from nexus.storage.operation_logger import OperationLogger

            with self.metadata.SessionLocal() as session:
                op_logger = OperationLogger(session)
                op_logger.log_operation(
                    operation_type="delete",
                    path=path,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    snapshot_hash=snapshot_hash,
                    metadata_snapshot=metadata_snapshot,
                    status="success",
                )
                session.commit()
        except Exception:
            # Don't fail the delete operation if logging fails
            pass

        # Delete from routed backend CAS (decrements ref count)
        # Content is only physically deleted when ref_count reaches 0
        # If other files reference the same content, it remains in CAS
        if meta.etag:
            route.backend.delete_content(meta.etag, context=context)

        # Remove from metadata
        self.metadata.delete(path)

        # v0.7.0: Fire workflow event for automatic trigger execution
        if self.enable_workflows and self.workflow_engine:  # type: ignore[attr-defined]
            import asyncio
            from datetime import UTC

            from nexus.workflows.types import TriggerType

            event_context = {
                "file_path": path,
                "size": meta.size,
                "etag": meta.etag,
                "tenant_id": tenant_id or "default",
                "agent_id": agent_id,
                "user_id": context.user_id if context and hasattr(context, "user_id") else None,
                "timestamp": datetime.now(UTC).isoformat(),
            }

            # Fire event asynchronously (don't block file delete)
            # No event loop running, skip workflow triggering
            with contextlib.suppress(RuntimeError):
                asyncio.create_task(
                    self.workflow_engine.fire_event(  # type: ignore[attr-defined]
                        TriggerType.FILE_DELETE, event_context
                    )
                )

    @rpc_expose(description="Rename/move file")
    def rename(self, old_path: str, new_path: str, context: OperationContext | None = None) -> None:
        """
        Rename/move a file by updating its path in metadata.

        This is a metadata-only operation that does NOT copy file content.
        The file's content remains in the same location in CAS storage,
        only the virtual path is updated in the metadata database.

        This makes rename/move operations instant, regardless of file size.

        Args:
            old_path: Current virtual path
            new_path: New virtual path
            context: Optional operation context for permission checks (uses default if not provided)

        Raises:
            NexusFileNotFoundError: If source file doesn't exist
            FileExistsError: If destination path already exists
            InvalidPathError: If either path is invalid
            PermissionError: If either path is read-only
            AccessDeniedError: If access is denied (tenant isolation)

        Example:
            >>> nx.rename('/workspace/old.txt', '/workspace/new.txt')
            >>> nx.rename('/folder-a/file.txt', '/shared/folder-a/file.txt')
        """
        old_path = self._validate_path(old_path)
        new_path = self._validate_path(new_path)

        # Route both paths
        tenant_id, agent_id, is_admin = self._get_routing_params(context)
        old_route = self.router.route(
            old_path,
            tenant_id=tenant_id,
            agent_id=agent_id,
            is_admin=is_admin,
            check_write=True,  # Need write access to source
        )
        new_route = self.router.route(
            new_path,
            tenant_id=tenant_id,
            agent_id=agent_id,
            is_admin=is_admin,
            check_write=True,  # Need write access to destination
        )

        # Check if paths are read-only
        if old_route.readonly:
            raise PermissionError(f"Cannot rename from read-only path: {old_path}")
        if new_route.readonly:
            raise PermissionError(f"Cannot rename to read-only path: {new_path}")

        # Check if source exists
        if not self.metadata.exists(old_path):
            raise NexusFileNotFoundError(old_path)

        # Capture snapshot before operation for undo capability
        meta = self.metadata.get(old_path)
        snapshot_hash = meta.etag if meta else None
        metadata_snapshot = None
        if meta:
            metadata_snapshot = {
                "size": meta.size,
                "version": meta.version,
                "modified_at": meta.modified_at.isoformat() if meta.modified_at else None,
            }

        # Check if destination already exists
        if self.metadata.exists(new_path):
            raise FileExistsError(f"Destination path already exists: {new_path}")

        # Check if this is a directory BEFORE renaming (important!)
        # After rename, the old path won't have children anymore
        is_directory = self.metadata.is_implicit_directory(old_path)

        # Perform metadata-only rename (no CAS I/O!)
        self.metadata.rename_path(old_path, new_path)

        # Update ReBAC permissions to follow the renamed file/directory
        # This ensures permissions are preserved when files are moved
        if hasattr(self, "_rebac_manager") and self._rebac_manager:
            try:
                import logging

                logger = logging.getLogger(__name__)
                logger.info(
                    f"Updating ReBAC permissions: {old_path} -> {new_path}, is_directory={is_directory}"
                )

                # Update all ReBAC tuples that reference this path
                updated_count = self._rebac_manager.update_object_path(
                    old_path=old_path,
                    new_path=new_path,
                    object_type="file",
                    is_directory=is_directory,
                )

                # Log if any permissions were updated
                logger.info(f"Updated {updated_count} ReBAC tuples")
                if updated_count > 0:
                    pass  # Successfully updated permissions silently
            except Exception as e:
                # Don't fail the rename operation if ReBAC update fails
                # The file is already renamed in metadata, we just couldn't update permissions
                import logging

                logger = logging.getLogger(__name__)
                logger.error(
                    f"Failed to update ReBAC permissions during rename: {e}", exc_info=True
                )
                pass

        # Log operation for audit trail and undo capability
        try:
            from nexus.storage.operation_logger import OperationLogger

            with self.metadata.SessionLocal() as session:
                op_logger = OperationLogger(session)
                op_logger.log_operation(
                    operation_type="rename",
                    path=old_path,
                    new_path=new_path,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    snapshot_hash=snapshot_hash,
                    metadata_snapshot=metadata_snapshot,
                    status="success",
                )
                session.commit()
        except Exception:
            # Don't fail the rename operation if logging fails
            pass

        # v0.7.0: Fire workflow event for automatic trigger execution
        if self.enable_workflows and self.workflow_engine:  # type: ignore[attr-defined]
            import asyncio
            from datetime import UTC

            from nexus.workflows.types import TriggerType

            event_context = {
                "old_path": old_path,
                "new_path": new_path,
                "size": meta.size if meta else 0,
                "etag": meta.etag if meta else None,
                "tenant_id": tenant_id or "default",
                "agent_id": agent_id,
                "user_id": context.user_id if context and hasattr(context, "user_id") else None,
                "timestamp": datetime.now(UTC).isoformat(),
            }

            # Fire event asynchronously (don't block file rename)
            # No event loop running, skip workflow triggering
            with contextlib.suppress(RuntimeError):
                asyncio.create_task(
                    self.workflow_engine.fire_event(  # type: ignore[attr-defined]
                        TriggerType.FILE_RENAME, event_context
                    )
                )

    @rpc_expose(description="Check if file exists")
    def exists(self, path: str, context: OperationContext | None = None) -> bool:
        """
        Check if a file or directory exists.

        Args:
            path: Virtual path to check
            context: Operation context for permission checks (uses default if None)

        Returns:
            True if file or implicit directory exists AND user has read permission on it
            OR any descendant (enables hierarchical navigation), False otherwise

        Note:
            With permissions enabled, directories are visible if user has access to ANY
            descendant, even if they don't have direct access to the directory itself.
            This enables hierarchical navigation (e.g., /workspace visible if user has
            access to /workspace/joe/file.txt).
        """
        try:
            path = self._validate_path(path)

            # Check read permission if enforcement enabled (with hierarchical descendant access)
            if self._enforce_permissions:  # type: ignore[attr-defined]
                ctx = context if context is not None else self._default_context
                # Use hierarchical access check: return True if user has access to path OR any descendant
                if not self._has_descendant_access(path, Permission.READ, ctx):  # type: ignore[attr-defined]
                    # No permission to path or any descendant = treat as non-existent for security
                    return False

            # Check if file exists explicitly
            if self.metadata.exists(path):
                return True
            # Check if it's an implicit directory (has files beneath it)
            return self.metadata.is_implicit_directory(path)
        except Exception:  # InvalidPathError
            return False

    def _compute_etag(self, content: bytes) -> str:
        """
        Compute ETag for file content.

        Args:
            content: File content

        Returns:
            ETag (MD5 hash)
        """
        return hashlib.md5(content).hexdigest()

    def _read_memory_path(
        self, path: str, return_metadata: bool = False, context: OperationContext | None = None
    ) -> bytes | dict[str, Any]:
        """Read memory via virtual path (Phase 2 Integration).

        Args:
            path: Memory virtual path.
            return_metadata: If True, return dict with content and metadata.

        Returns:
            Memory content as bytes, or dict with metadata if return_metadata=True.

        Raises:
            NexusFileNotFoundError: If memory doesn't exist.
        """
        from nexus.core.entity_registry import EntityRegistry
        from nexus.core.memory_router import MemoryViewRouter

        # Get memory via router
        session = self.metadata.SessionLocal()
        try:
            router = MemoryViewRouter(session, EntityRegistry(session))
            memory = router.resolve(path)

            if not memory:
                raise NexusFileNotFoundError(f"Memory not found at path: {path}")

            # Read content from CAS
            content = self.backend.read_content(memory.content_hash, context=context)

            if return_metadata:
                return {
                    "content": content,
                    "etag": memory.content_hash,
                    "version": 1,  # Memories don't version like files
                    "modified_at": memory.created_at,
                    "size": len(content),
                }

            return content
        finally:
            session.close()

    def _write_memory_path(self, path: str, content: bytes) -> dict[str, Any]:
        """Write memory via virtual path (Phase 2 Integration).

        Args:
            path: Memory virtual path.
            content: Content to store.

        Returns:
            Dict with memory metadata.
        """
        # Delegate to Memory API
        if not hasattr(self, "memory") or self.memory is None:
            raise RuntimeError(
                "Memory API not initialized. Use nx.memory for direct memory operations."
            )

        # Extract memory type from path if present
        parts = [p for p in path.split("/") if p]
        memory_type = None
        if "memory" in parts:
            idx = parts.index("memory")
            if idx + 1 < len(parts):
                memory_type = parts[idx + 1]

        # Store memory with default scope='user'
        memory_id = self.memory.store(
            content=content.decode("utf-8") if isinstance(content, bytes) else content,
            scope="user",
            memory_type=memory_type,
        )

        # Get the created memory
        mem = self.memory.get(memory_id)

        # Handle case where memory.get() returns None
        if mem is None:
            raise RuntimeError(
                f"Failed to retrieve stored memory (id={memory_id}). "
                "The memory API may not be properly configured or the memory was not persisted."
            )

        return {
            "etag": mem["content_hash"],
            "version": 1,
            "modified_at": mem["created_at"],
            "size": len(content),
        }

    def _delete_memory_path(self, path: str, context: OperationContext | None = None) -> None:
        """Delete memory via virtual path (Phase 2 Integration).

        Args:
            path: Memory virtual path.

        Raises:
            NexusFileNotFoundError: If memory doesn't exist.
        """
        from nexus.core.entity_registry import EntityRegistry
        from nexus.core.memory_router import MemoryViewRouter

        # Get memory via router
        session = self.metadata.SessionLocal()
        try:
            router = MemoryViewRouter(session, EntityRegistry(session))
            memory = router.resolve(path)

            if not memory:
                raise NexusFileNotFoundError(f"Memory not found at path: {path}")

            # Delete the memory
            router.delete_memory(memory.memory_id)

            # Also delete content from CAS (decrement ref count)
            self.backend.delete_content(memory.content_hash, context=context)
        finally:
            session.close()

    @rpc_expose(description="Shutdown background parser threads")
    def shutdown_parser_threads(self, timeout: float = 10.0) -> dict[str, Any]:
        """Gracefully shutdown background parser threads.

        CRITICAL: Must be called before closing NexusFS to prevent database corruption!
        Non-daemon parser threads can have in-progress database writes that must complete.

        This method waits for all parser threads to finish or times out after the specified
        duration. This prevents abrupt termination that could corrupt the database.

        Args:
            timeout: Maximum seconds to wait for each thread to finish (default: 10s)

        Returns:
            Dict with shutdown statistics:
                - total_threads: Number of parser threads that were running
                - completed: Number of threads that finished gracefully
                - timed_out: Number of threads that exceeded timeout
                - timeout_threads: List of thread names that timed out

        Example:
            >>> nx = NexusFS(...)
            >>> # ... use filesystem ...
            >>> stats = nx.shutdown_parser_threads(timeout=5.0)
            >>> if stats['timed_out'] > 0:
            ...     logger.warning(f"{stats['timed_out']} parser threads timed out")
            >>> nx.close()
        """
        with self._parser_threads_lock:
            threads_to_wait = [t for t in self._parser_threads if t.is_alive()]
            total = len(threads_to_wait)

        if total == 0:
            return {"total_threads": 0, "completed": 0, "timed_out": 0, "timeout_threads": []}

        logger.info(f"Waiting for {total} parser threads to complete (timeout: {timeout}s)...")

        completed = 0
        timed_out = 0
        timeout_threads = []

        for thread in threads_to_wait:
            logger.debug(f"Waiting for parser thread: {thread.name}")
            thread.join(timeout=timeout)

            if thread.is_alive():
                # Thread exceeded timeout
                timed_out += 1
                timeout_threads.append(thread.name)
                logger.warning(
                    f"Parser thread '{thread.name}' did not complete within {timeout}s. "
                    f"Thread may still be writing to database - potential data loss risk!"
                )
            else:
                # Thread completed successfully
                completed += 1
                logger.debug(f"Parser thread '{thread.name}' completed")

        # Clear the thread list
        with self._parser_threads_lock:
            self._parser_threads.clear()

        logger.info(
            f"Parser thread shutdown complete: {completed} completed, {timed_out} timed out"
        )

        return {
            "total_threads": total,
            "completed": completed,
            "timed_out": timed_out,
            "timeout_threads": timeout_threads,
        }
