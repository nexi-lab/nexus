"""Protocols for the skills brick.

Defines narrow interfaces that the skills module depends on:

- ``NexusFilesystem``: 7-method filesystem protocol (sys_read/sys_write/sys_readdir/…)
- ``SkillRegistryProtocol``: skill discovery, lookup, and dependency resolution
- ``SkillManagerProtocol``: skill lifecycle (create, fork, publish, search)

Callers should type-hint against Protocols, not concrete classes.
Concrete implementations: ``SkillRegistry``, ``SkillManager``.

.. note::
    ``NexusFilesystem`` canonical location is ``nexus.services.protocols.filesystem``.
    Re-exported here for backward compatibility.

Verification:
- Run: pytest tests/unit/skills/test_protocol_compatibility.py
- Contract test verifies NexusFilesystem ABC satisfies this protocol
"""

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from nexus.services.protocols.filesystem import NexusFilesystem

if TYPE_CHECKING:
    from nexus.bricks.skills.models import Skill, SkillMetadata


@runtime_checkable
class SkillRegistryProtocol(Protocol):
    """Protocol for skill registry operations.

    Concrete implementation: ``nexus.bricks.skills.registry.SkillRegistry``.
    """

    async def discover(
        self,
        context: Any = None,
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
        context: Any = None,
        load_dependencies: bool = False,
    ) -> "Skill":
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
    ) -> "list[str] | list[SkillMetadata]":
        """List available skills from the discovered index."""
        ...

    def get_metadata(self, name: str) -> "SkillMetadata":
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


@runtime_checkable
class SkillManagerProtocol(Protocol):
    """Protocol for skill lifecycle management.

    Concrete implementation: ``nexus.bricks.skills.manager.SkillManager``.
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
        context: Any = None,
        **kwargs: str,
    ) -> str:
        """Create a new skill from a template."""
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
        context: Any = None,
    ) -> str:
        """Create a new skill from raw content."""
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
        """Fork an existing skill with lineage tracking."""
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
        """Publish a skill to a wider audience."""
        ...

    async def search_skills(
        self,
        query: str,
        tier: str | None = None,
        limit: int | None = 10,
    ) -> list[tuple[str, float]]:
        """Search skills by description using text matching."""
        ...


__all__ = ["NexusFilesystem", "SkillManagerProtocol", "SkillRegistryProtocol"]
