"""Protocols for the skills module.

Defines narrow interfaces that the skills module depends on:

- ``NexusFilesystem``: 7-method filesystem protocol (read/write/list/…)
- ``SkillRegistryProtocol``: skill discovery, lookup, and dependency resolution
- ``SkillManagerProtocol``: skill lifecycle (create, fork, publish, search)

Callers should type-hint against Protocols, not concrete classes.
Concrete implementations: ``SkillRegistry``, ``SkillManager``.

Verification:
- Run: pytest tests/unit/skills/test_protocol_compatibility.py
- Contract test verifies NexusFilesystem ABC satisfies this protocol
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext
    from nexus.skills.models import Skill, SkillMetadata


@runtime_checkable
class NexusFilesystem(Protocol):
    """Narrow filesystem protocol for the skills module.

    Contains only the methods used by skills and MCP code:
    - read: Read file content
    - write: Write file content
    - list: List files in a directory
    - exists: Check if a path exists
    - mkdir: Create a directory
    - delete: Delete a file
    - is_directory: Check if path is a directory
    """

    def read(
        self, path: str, context: Any = None, return_metadata: bool = False
    ) -> bytes | dict[str, Any]:
        """Read file content.

        Args:
            path: Virtual path to read
            context: Optional operation context for permission checks
            return_metadata: If True, return dict with content and metadata

        Returns:
            File content as bytes (default) or dict with metadata
        """
        ...

    def write(
        self,
        path: str,
        content: bytes,
        context: Any = None,
        if_match: str | None = None,
        if_none_match: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """Write content to a file.

        Args:
            path: Virtual path to write
            content: File content as bytes
            context: Optional operation context for permission checks
            if_match: Etag for optimistic concurrency
            if_none_match: Fail if file already exists
            force: Skip version check

        Returns:
            Dict with metadata (etag, version, modified_at, size)
        """
        ...

    def list(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        show_parsed: bool = True,
        context: Any = None,
    ) -> list[str] | list[dict[str, Any]]:
        """List files in a directory.

        Args:
            path: Directory path to list
            recursive: If True, list all files recursively
            details: If True, return detailed metadata
            show_parsed: If True, include virtual parsed views
            context: Optional operation context

        Returns:
            List of file paths or metadata dicts
        """
        ...

    def exists(self, path: str) -> bool:
        """Check if a file or directory exists.

        Args:
            path: Virtual path to check

        Returns:
            True if path exists
        """
        ...

    def mkdir(self, path: str, parents: bool = False, exist_ok: bool = False) -> None:
        """Create a directory.

        Args:
            path: Virtual path to directory
            parents: Create parent directories if needed
            exist_ok: Don't raise if directory exists
        """
        ...

    def delete(self, path: str) -> None:
        """Delete a file.

        Args:
            path: Virtual path to delete
        """
        ...

    def is_directory(self, path: str, context: Any = None) -> bool:
        """Check if path is a directory.

        Args:
            path: Virtual path to check
            context: Optional operation context

        Returns:
            True if path is a directory
        """
        ...


# ---------------------------------------------------------------------------
# SkillRegistryProtocol — discovery, lookup, dependency resolution
# ---------------------------------------------------------------------------


@runtime_checkable
class SkillRegistryProtocol(Protocol):
    """Protocol for skill registry operations.

    Concrete implementation: ``nexus.skills.registry.SkillRegistry``.
    """

    async def discover(
        self,
        context: OperationContext | None = None,
        tiers: list[str] | None = None,
    ) -> int:
        """Discover skills from filesystem (metadata only).

        Returns:
            Number of skills discovered.
        """
        ...

    async def get_skill(
        self,
        name: str,
        context: OperationContext | None = None,
        load_dependencies: bool = False,
    ) -> Skill:
        """Get a skill by name (loads full content on-demand).

        Raises:
            SkillNotFoundError: If skill not found.
            SkillPermissionDeniedError: If subject lacks read permission.
        """
        ...

    async def resolve_dependencies(self, name: str) -> list[str]:
        """Resolve all dependencies for a skill (DAG order).

        Raises:
            SkillNotFoundError: If skill or dependency not found.
            SkillDependencyError: If circular dependency detected.
        """
        ...

    def list_skills(
        self,
        tier: str | None = None,
        include_metadata: bool = False,
    ) -> list[str] | list[SkillMetadata]:
        """List available skills from the discovered index."""
        ...

    def get_metadata(self, name: str) -> SkillMetadata:
        """Get skill metadata without loading full content.

        Raises:
            SkillNotFoundError: If skill not found.
        """
        ...

    def clear_cache(self) -> None:
        """Clear the loaded-skill cache (metadata index preserved)."""
        ...

    def clear(self) -> None:
        """Clear all registered skills and caches."""
        ...


# ---------------------------------------------------------------------------
# SkillManagerProtocol — lifecycle (create, fork, publish, search)
# ---------------------------------------------------------------------------


@runtime_checkable
class SkillManagerProtocol(Protocol):
    """Protocol for skill lifecycle management.

    Concrete implementation: ``nexus.skills.manager.SkillManager``.
    """

    async def create_skill(
        self,
        name: str,
        description: str,
        template: str = "basic",
        tier: str = "user",
        author: str | None = None,
        version: str = "1.0.0",
        creator_id: str | None = None,
        creator_type: str = "agent",
        zone_id: str | None = None,
        context: OperationContext | None = None,
        **kwargs: str,
    ) -> str:
        """Create a new skill from a template.

        Returns:
            Path to created SKILL.md file.
        """
        ...

    async def create_skill_from_content(
        self,
        name: str,
        description: str,
        content: str,
        tier: str = "user",
        author: str | None = None,
        version: str = "1.0.0",
        source_url: str | None = None,
        metadata: dict[str, Any] | None = None,
        context: OperationContext | None = None,
    ) -> str:
        """Create a new skill from raw content.

        Returns:
            Path to created SKILL.md file.
        """
        ...

    async def fork_skill(
        self,
        source_name: str,
        target_name: str,
        tier: str = "user",
        author: str | None = None,
        creator_id: str | None = None,
        creator_type: str = "user",
        zone_id: str | None = None,
    ) -> str:
        """Fork an existing skill with lineage tracking.

        Returns:
            Path to forked SKILL.md file.
        """
        ...

    async def publish_skill(
        self,
        name: str,
        source_tier: str = "agent",
        target_tier: str = "zone",
        publisher_id: str | None = None,
        publisher_type: str = "agent",
        zone_id: str | None = None,
    ) -> str:
        """Publish a skill to a wider audience.

        Returns:
            Path to published SKILL.md file.
        """
        ...

    async def search_skills(
        self,
        query: str,
        tier: str | None = None,
        limit: int | None = 10,
    ) -> list[tuple[str, float]]:
        """Search skills by description using text matching.

        Returns:
            List of (skill_name, score) tuples sorted by relevance.
        """
        ...
