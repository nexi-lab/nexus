"""Memory versioning service (#1498).

Extracts version tracking, rollback, diff, and history operations from
Memory.store() into a composable, independently testable class.

Handles the supersedes chain traversal, version history queries, and
garbage collection of old versions.

Usage:
    versioning = MemoryVersioning(session_factory, memory_router, permission_enforcer, backend, context)
    versions = versioning.list_versions("mem_123")
    versioning.rollback("mem_123", version=1)
"""

from __future__ import annotations

import builtins
import difflib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from nexus.bricks.rebac.memory_permission_enforcer import MemoryPermissionEnforcer
from nexus.contracts.types import OperationContext, Permission
from nexus.services.memory.memory_router import MemoryViewRouter

logger = logging.getLogger(__name__)


class MemoryVersioning:
    """Version tracking operations for memories (#1184, #1188).

    Manages the supersedes chain, version history, rollback,
    diff comparisons, and garbage collection.
    """

    def __init__(
        self,
        session_factory: Callable[..., Any],
        memory_router: MemoryViewRouter,
        permission_enforcer: MemoryPermissionEnforcer,
        backend: Any,
        context: OperationContext,
    ) -> None:
        self._session_factory = session_factory
        self._memory_router = memory_router
        self._permission_enforcer = permission_enforcer
        self._backend = backend
        self._context = context

    def resolve_to_current(self, memory_id: str) -> Any:
        """Follow the superseded_by chain to find the current memory (#1188).

        If the given memory_id has been superseded, follows the chain forward
        to find the latest (current) version.

        Returns:
            The current MemoryModel, or None if not found.
        """
        memory = self._memory_router._get_memory_by_id_raw(memory_id)
        if not memory:
            return None

        visited = {memory.memory_id}
        while memory.superseded_by_id:
            successor = self._memory_router._get_memory_by_id_raw(memory.superseded_by_id)
            if successor is None or successor.memory_id in visited:
                break
            visited.add(successor.memory_id)
            memory = successor

        return memory

    def get_chain_memory_ids(self, memory_id: str) -> builtins.list[str]:
        """Get all memory IDs in the supersedes chain (#1188).

        Walks backward to find the oldest ancestor, then forward to collect all IDs.

        Args:
            memory_id: Any memory ID in the chain.

        Returns:
            List of all memory IDs in the chain (oldest to newest).
        """
        memory = self._memory_router._get_memory_by_id_raw(memory_id)
        if not memory:
            return [memory_id]

        # Walk backward to oldest ancestor
        current = memory
        while current.supersedes_id:
            ancestor = self._memory_router._get_memory_by_id_raw(current.supersedes_id)
            if ancestor is None:
                break
            current = ancestor

        # Walk forward collecting all IDs
        chain_ids: builtins.list[str] = []
        visited: set[str] = set()
        node = current
        while node and node.memory_id not in visited:
            visited.add(node.memory_id)
            chain_ids.append(node.memory_id)
            if node.superseded_by_id:
                next_node = self._memory_router._get_memory_by_id_raw(node.superseded_by_id)
                if next_node is None:
                    break
                node = next_node
            else:
                break

        return chain_ids

    def list_versions(self, memory_id: str) -> builtins.list[dict[str, Any]]:
        """List all versions of a memory.

        Returns version history ordered by version number (newest first).
        Follows the supersedes chain (#1188) to collect versions across all memory rows.

        Args:
            memory_id: The memory ID to get versions for.

        Returns:
            List of version info dicts.
        """
        from sqlalchemy import select

        from nexus.storage.models import VersionHistoryModel

        chain_ids = self.get_chain_memory_ids(memory_id)

        stmt = (
            select(VersionHistoryModel)
            .where(
                VersionHistoryModel.resource_type == "memory",
                VersionHistoryModel.resource_id.in_(chain_ids),
            )
            .order_by(VersionHistoryModel.version_number.desc())
        )

        session = self._session_factory()
        try:
            versions: builtins.list[dict[str, Any]] = []
            for v in session.scalars(stmt):
                versions.append(
                    {
                        "version": v.version_number,
                        "content_hash": v.content_hash,
                        "size": v.size_bytes,
                        "mime_type": v.mime_type,
                        "created_at": v.created_at.isoformat() if v.created_at else None,
                        "created_by": v.created_by,
                        "change_reason": v.change_reason,
                        "source_type": v.source_type,
                        "parent_version_id": v.parent_version_id,
                    }
                )

            return versions
        finally:
            session.close()

    def get_version(
        self,
        memory_id: str,
        version: int,
        context: OperationContext | None = None,
    ) -> dict[str, Any] | None:
        """Retrieve a specific version of a memory.

        Fetches the content and metadata for a specific historical version
        using CAS storage.

        Args:
            memory_id: The memory ID.
            version: Version number to retrieve (1-indexed).
            context: Optional operation context for permission checks.

        Returns:
            Memory dictionary with content at specified version, or None.
        """
        from sqlalchemy import select

        from nexus.storage.models import VersionHistoryModel

        memory = self._memory_router.get_memory_by_id(memory_id)
        if not memory:
            return None

        check_context = context or self._context
        if not self._permission_enforcer.check_memory(memory, Permission.READ, check_context):
            return None

        chain_ids = self.get_chain_memory_ids(memory_id)

        stmt = select(VersionHistoryModel).where(
            VersionHistoryModel.resource_type == "memory",
            VersionHistoryModel.resource_id.in_(chain_ids),
            VersionHistoryModel.version_number == version,
        )
        session = self._session_factory()
        try:
            version_entry = session.scalar(stmt)

            if not version_entry:
                return None

            # Read content from CAS
            content = self._read_version_content(version_entry.content_hash)

            return {
                "memory_id": memory_id,
                "version": version_entry.version_number,
                "content": content,
                "content_hash": version_entry.content_hash,
                "size": version_entry.size_bytes,
                "mime_type": version_entry.mime_type,
                "created_at": version_entry.created_at.isoformat()
                if version_entry.created_at
                else None,
                "created_by": version_entry.created_by,
                "source_type": version_entry.source_type,
                "change_reason": version_entry.change_reason,
            }
        finally:
            session.close()

    def rollback(
        self,
        memory_id: str,
        version: int,
        context: OperationContext | None = None,
    ) -> None:
        """Rollback a memory to a previous version.

        Restores the memory content to a specific historical version.
        Creates a new version entry with source_type='rollback' to maintain
        audit trail.

        Args:
            memory_id: The memory ID to rollback.
            version: Version number to rollback to.
            context: Optional operation context for permission checks.

        Raises:
            ValueError: If memory or version not found, or no permission.
        """
        from sqlalchemy import select, update

        from nexus.storage.models import MemoryModel, VersionHistoryModel

        memory = self._memory_router.get_memory_by_id(memory_id)
        if not memory:
            raise ValueError(f"Memory not found: {memory_id}")

        check_context = context or self._context
        if not self._permission_enforcer.check_memory(memory, Permission.WRITE, check_context):
            raise ValueError(f"No permission to rollback memory: {memory_id}")

        chain_ids = self.get_chain_memory_ids(memory_id)

        # Find the latest (current) memory in the chain
        latest_memory = self._memory_router.get_memory_by_id(chain_ids[-1])
        if latest_memory is None:
            latest_memory = memory
        latest_memory_id = latest_memory.memory_id

        session = self._session_factory()
        try:
            target_stmt = select(VersionHistoryModel).where(
                VersionHistoryModel.resource_type == "memory",
                VersionHistoryModel.resource_id.in_(chain_ids),
                VersionHistoryModel.version_number == version,
            )
            target_version = session.scalar(target_stmt)

            if not target_version:
                raise ValueError(f"Version {version} not found for memory {memory_id}")

            # Get current version entry for lineage
            current_stmt = select(VersionHistoryModel).where(
                VersionHistoryModel.resource_type == "memory",
                VersionHistoryModel.resource_id.in_(chain_ids),
                VersionHistoryModel.version_number == latest_memory.current_version,
            )
            current_version_entry = session.scalar(current_stmt)
            parent_version_id = current_version_entry.version_id if current_version_entry else None

            # Update the latest memory to target version's content
            latest_memory.content_hash = target_version.content_hash
            latest_memory.updated_at = datetime.now(UTC)

            # Atomically increment version at database level
            session.execute(
                update(MemoryModel)
                .where(MemoryModel.memory_id == latest_memory_id)
                .values(current_version=MemoryModel.current_version + 1)
            )
            session.refresh(latest_memory)

            # Create version history entry for the rollback
            self._memory_router._create_version_entry(
                memory_id=latest_memory_id,
                content_hash=target_version.content_hash,
                size_bytes=target_version.size_bytes,
                version_number=latest_memory.current_version,
                source_type="rollback",
                parent_version_id=parent_version_id,
                change_reason=f"Rollback to version {version}",
                created_by=check_context.user_id if check_context else None,
            )

            session.commit()
        finally:
            session.close()

    def diff_versions(
        self,
        memory_id: str,
        v1: int,
        v2: int,
        mode: Literal["metadata", "content"] = "metadata",
        context: OperationContext | None = None,
    ) -> dict[str, Any] | str:
        """Compare two versions of a memory.

        Args:
            memory_id: The memory ID.
            v1: First version number.
            v2: Second version number.
            mode: "metadata" for size/hash comparison, "content" for unified diff.
            context: Optional operation context for permission checks.

        Returns:
            For mode="metadata": Dict with version comparison info.
            For mode="content": String in unified diff format.

        Raises:
            ValueError: If memory or versions not found, or no permission.
        """
        from sqlalchemy import select

        from nexus.storage.models import VersionHistoryModel

        memory = self._memory_router.get_memory_by_id(memory_id)
        if not memory:
            raise ValueError(f"Memory not found: {memory_id}")

        check_context = context or self._context
        if not self._permission_enforcer.check_memory(memory, Permission.READ, check_context):
            raise ValueError(f"No permission to diff memory: {memory_id}")

        chain_ids = self.get_chain_memory_ids(memory_id)

        stmt = select(VersionHistoryModel).where(
            VersionHistoryModel.resource_type == "memory",
            VersionHistoryModel.resource_id.in_(chain_ids),
            VersionHistoryModel.version_number.in_([v1, v2]),
        )

        session = self._session_factory()
        try:
            versions_dict = {v.version_number: v for v in session.scalars(stmt)}

            if v1 not in versions_dict:
                raise ValueError(f"Version {v1} not found for memory {memory_id}")
            if v2 not in versions_dict:
                raise ValueError(f"Version {v2} not found for memory {memory_id}")

            version1 = versions_dict[v1]
            version2 = versions_dict[v2]

            if mode == "metadata":
                return {
                    "memory_id": memory_id,
                    "v1": v1,
                    "v2": v2,
                    "content_hash_v1": version1.content_hash,
                    "content_hash_v2": version2.content_hash,
                    "content_changed": version1.content_hash != version2.content_hash,
                    "size_v1": version1.size_bytes,
                    "size_v2": version2.size_bytes,
                    "size_delta": version2.size_bytes - version1.size_bytes,
                    "created_at_v1": version1.created_at.isoformat()
                    if version1.created_at
                    else None,
                    "created_at_v2": version2.created_at.isoformat()
                    if version2.created_at
                    else None,
                }

            # Content diff mode
            raw1 = self._read_version_content(version1.content_hash)
            raw2 = self._read_version_content(version2.content_hash)
        finally:
            session.close()

        content1 = raw1 if isinstance(raw1, str) else str(raw1)
        content2 = raw2 if isinstance(raw2, str) else str(raw2)

        diff_lines = difflib.unified_diff(
            content1.splitlines(keepends=True),
            content2.splitlines(keepends=True),
            fromfile=f"version {v1}",
            tofile=f"version {v2}",
        )
        return "".join(diff_lines)

    def get_history(self, memory_id: str) -> builtins.list[dict[str, Any]]:
        """Get the complete version history chain for a memory (#1188).

        Traverses the supersedes chain to return all versions,
        from oldest to newest.

        Args:
            memory_id: Any memory ID in the chain.

        Returns:
            List of memory dicts in chronological order (oldest first).
        """
        memory = self._memory_router.get_memory_by_id(memory_id)
        if not memory:
            return []

        # Walk backward to find the oldest ancestor
        current = memory
        while current.supersedes_id:
            ancestor = self._memory_router.get_memory_by_id(current.supersedes_id)
            if ancestor is None:
                break
            current = ancestor

        # Walk forward from oldest to newest
        chain: builtins.list[dict[str, Any]] = []
        visited: set[str] = set()
        node = current
        while node and node.memory_id not in visited:
            visited.add(node.memory_id)

            content = self._read_version_content(node.content_hash, parse_json=True)

            chain.append(
                {
                    "memory_id": node.memory_id,
                    "content": content,
                    "content_hash": node.content_hash,
                    "version": node.current_version,
                    "supersedes_id": node.supersedes_id,
                    "superseded_by_id": node.superseded_by_id,
                    "valid_at": node.valid_at.isoformat() if node.valid_at else None,
                    "invalid_at": node.invalid_at.isoformat() if node.invalid_at else None,
                    "created_at": node.created_at.isoformat() if node.created_at else None,
                }
            )

            if node.superseded_by_id:
                next_node = self._memory_router.get_memory_by_id(node.superseded_by_id)
                if next_node is None:
                    break
                node = next_node
            else:
                break

        return chain

    def gc_old_versions(self, older_than_days: int = 365) -> int:
        """Garbage collect old superseded versions (#1188).

        Removes superseded memories older than the threshold.
        Never removes current (non-superseded) memories.

        Args:
            older_than_days: Only remove versions older than this many days.

        Returns:
            Number of versions removed.
        """
        from datetime import timedelta

        from sqlalchemy import select

        from nexus.storage.models import MemoryModel

        now = datetime.now(UTC)
        threshold = now - timedelta(days=older_than_days)

        stmt = select(MemoryModel).where(
            MemoryModel.superseded_by_id.isnot(None),
            MemoryModel.invalid_at.isnot(None),
            MemoryModel.invalid_at <= threshold,
        )
        session = self._session_factory()
        try:
            old_memories = list(session.execute(stmt).scalars().all())

            removed = 0
            for memory in old_memories:
                session.delete(memory)
                removed += 1

            if removed > 0:
                session.commit()

            return removed
        finally:
            session.close()

    def _read_version_content(
        self, content_hash: str, *, parse_json: bool = False
    ) -> str | dict[str, Any]:
        """Read content from CAS backend for version display.

        Args:
            content_hash: The CAS content hash.
            parse_json: If True, try to parse as JSON first.

        Returns:
            Content as string, dict (if parse_json), or error placeholder.
        """
        import json

        try:
            content_bytes: bytes = self._backend.read_content(
                content_hash, context=self._context
            ).unwrap()
            if parse_json:
                try:
                    parsed: dict[str, Any] = json.loads(content_bytes.decode("utf-8"))
                    return parsed
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
            try:
                return content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                return content_bytes.hex()
        except Exception:
            return f"<content not available: {content_hash}>"
