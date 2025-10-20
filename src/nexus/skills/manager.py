"""Skill lifecycle management: create, fork, publish, and versioning."""

import contextlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from nexus.core.exceptions import ValidationError
from nexus.skills.models import SkillMetadata
from nexus.skills.parser import SkillParser
from nexus.skills.protocols import NexusFilesystem
from nexus.skills.registry import SkillNotFoundError, SkillRegistry

logger = logging.getLogger(__name__)


class SkillManagerError(ValidationError):
    """Raised when skill management operations fail."""

    pass


class SkillManager:
    """Manager for skill lifecycle operations.

    Features:
    - Create skills from templates
    - Fork existing skills with lineage tracking
    - Publish skills to tenant library
    - Version control via CAS (Content Addressable Storage)

    Example:
        >>> from nexus import connect
        >>> from nexus.skills import SkillRegistry, SkillManager
        >>>
        >>> nx = connect()
        >>> registry = SkillRegistry(nx)
        >>> manager = SkillManager(nx, registry)
        >>>
        >>> # Create new skill from template
        >>> await manager.create_skill(
        ...     "my-skill",
        ...     description="My custom skill",
        ...     template="basic",
        ...     tier="agent"
        ... )
        >>>
        >>> # Fork existing skill
        >>> await manager.fork_skill(
        ...     "analyze-code",
        ...     "my-analyzer",
        ...     tier="agent"
        ... )
        >>>
        >>> # Publish to tenant library
        >>> await manager.publish_skill("my-skill")
    """

    def __init__(
        self,
        filesystem: NexusFilesystem | None = None,
        registry: SkillRegistry | None = None,
    ):
        """Initialize skill manager.

        Args:
            filesystem: Optional filesystem instance (defaults to local FS)
            registry: Optional skill registry for loading existing skills
        """
        self._filesystem = filesystem
        self._registry = registry or SkillRegistry(filesystem)
        self._parser = SkillParser()

    async def create_skill(
        self,
        name: str,
        description: str,
        template: str = "basic",
        tier: str = "agent",
        author: str | None = None,
        version: str = "1.0.0",
        **kwargs: str,
    ) -> str:
        """Create a new skill from a template.

        Args:
            name: Skill name (alphanumeric with - or _)
            description: Skill description
            template: Template name (basic, data-analysis, code-generation, etc.)
            tier: Target tier (agent, tenant, system)
            author: Optional author name
            version: Initial version (default: 1.0.0)
            **kwargs: Additional template variables

        Returns:
            Path to created SKILL.md file

        Raises:
            SkillManagerError: If creation fails

        Example:
            >>> path = await manager.create_skill(
            ...     "my-analyzer",
            ...     description="Analyzes code quality",
            ...     template="code-generation",
            ...     author="Alice"
            ... )
        """
        # Validate skill name
        if not name.replace("-", "").replace("_", "").isalnum():
            raise SkillManagerError(f"Skill name must be alphanumeric (with - or _), got '{name}'")

        # Validate tier
        if tier not in SkillRegistry.TIER_PATHS:
            raise SkillManagerError(
                f"Invalid tier '{tier}'. Must be one of: {list(SkillRegistry.TIER_PATHS.keys())}"
            )

        # Get tier path
        tier_path = SkillRegistry.TIER_PATHS[tier]

        # Construct skill directory path
        skill_dir = f"{tier_path}{name}/"
        skill_file = f"{skill_dir}SKILL.md"

        # Check if skill already exists
        if self._filesystem:
            if self._filesystem.exists(skill_file):
                raise SkillManagerError(f"Skill '{name}' already exists at {skill_file}")
        else:
            local_path = Path(skill_file)
            if local_path.exists():
                raise SkillManagerError(f"Skill '{name}' already exists at {skill_file}")

        # Load template
        from nexus.skills.templates import get_template

        template_content = get_template(template, name=name, description=description, **kwargs)

        # Create skill metadata
        now = datetime.utcnow()
        metadata = SkillMetadata(
            name=name,
            description=description,
            version=version,
            author=author,
            created_at=now,
            modified_at=now,
            tier=tier,
        )

        # Generate SKILL.md content
        import yaml

        frontmatter = {
            "name": metadata.name,
            "description": metadata.description,
            "version": metadata.version,
        }

        if author:
            frontmatter["author"] = author

        frontmatter["created_at"] = now.isoformat()
        frontmatter["modified_at"] = now.isoformat()

        frontmatter_yaml = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False)
        skill_md = f"---\n{frontmatter_yaml}---\n\n{template_content}"

        # Write skill file
        if self._filesystem:
            # Create directory
            with contextlib.suppress(Exception):
                self._filesystem.mkdir(skill_dir, parents=True)

            # Write file
            self._filesystem.write(skill_file, skill_md.encode("utf-8"))
        else:
            # Use local filesystem
            local_dir = Path(skill_dir)
            local_dir.mkdir(parents=True, exist_ok=True)
            Path(skill_file).write_text(skill_md, encoding="utf-8")

        logger.info(f"Created skill '{name}' from template '{template}' at {skill_file}")
        return skill_file

    async def fork_skill(
        self,
        source_name: str,
        target_name: str,
        tier: str = "agent",
        author: str | None = None,
    ) -> str:
        """Fork an existing skill with lineage tracking.

        Creates a copy of the source skill with:
        - New name
        - Updated metadata (forked_from, parent_skill)
        - New creation timestamp
        - Incremented version

        Args:
            source_name: Name of skill to fork
            target_name: Name for the forked skill
            tier: Target tier for the fork (default: agent)
            author: Optional author name for the fork

        Returns:
            Path to forked SKILL.md file

        Raises:
            SkillNotFoundError: If source skill not found
            SkillManagerError: If fork fails

        Example:
            >>> path = await manager.fork_skill(
            ...     "analyze-code",
            ...     "my-code-analyzer",
            ...     author="Bob"
            ... )
        """
        # Load source skill
        try:
            source_skill = await self._registry.get_skill(source_name)
        except SkillNotFoundError as e:
            raise SkillManagerError(f"Source skill '{source_name}' not found") from e

        # Validate target name
        if not target_name.replace("-", "").replace("_", "").isalnum():
            raise SkillManagerError(
                f"Skill name must be alphanumeric (with - or _), got '{target_name}'"
            )

        # Validate tier
        if tier not in SkillRegistry.TIER_PATHS:
            raise SkillManagerError(
                f"Invalid tier '{tier}'. Must be one of: {list(SkillRegistry.TIER_PATHS.keys())}"
            )

        # Get tier path
        tier_path = SkillRegistry.TIER_PATHS[tier]

        # Construct target path
        target_dir = f"{tier_path}{target_name}/"
        target_file = f"{target_dir}SKILL.md"

        # Check if target already exists
        if self._filesystem:
            if self._filesystem.exists(target_file):
                raise SkillManagerError(f"Skill '{target_name}' already exists at {target_file}")
        else:
            if Path(target_file).exists():
                raise SkillManagerError(f"Skill '{target_name}' already exists at {target_file}")

        # Create forked metadata
        now = datetime.utcnow()

        # Increment version (if source has version)
        new_version = source_skill.metadata.version or "1.0.0"
        if source_skill.metadata.version:
            # Simple version increment: 1.0.0 -> 1.1.0
            parts = source_skill.metadata.version.split(".")
            if len(parts) == 3:
                parts[1] = str(int(parts[1]) + 1)
                parts[2] = "0"
                new_version = ".".join(parts)

        # Build frontmatter
        import yaml

        frontmatter: dict[str, Any] = {
            "name": target_name,
            "description": source_skill.metadata.description,
            "version": new_version,
        }

        if author:
            frontmatter["author"] = author
        elif source_skill.metadata.author:
            frontmatter["author"] = source_skill.metadata.author

        # Add lineage tracking
        frontmatter["forked_from"] = source_name
        frontmatter["parent_skill"] = source_name

        # Add dependencies (preserve from source)
        if source_skill.metadata.requires:
            frontmatter["requires"] = source_skill.metadata.requires

        frontmatter["created_at"] = now.isoformat()
        frontmatter["modified_at"] = now.isoformat()

        # Preserve additional metadata from source
        frontmatter.update(source_skill.metadata.metadata)

        frontmatter_yaml = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False)
        skill_md = f"---\n{frontmatter_yaml}---\n\n{source_skill.content}"

        # Write forked skill
        if self._filesystem:
            # Create directory
            with contextlib.suppress(Exception):
                self._filesystem.mkdir(target_dir, parents=True)

            # Write file (CAS deduplication will happen automatically in NexusFS)
            self._filesystem.write(target_file, skill_md.encode("utf-8"))
        else:
            # Use local filesystem
            local_dir = Path(target_dir)
            local_dir.mkdir(parents=True, exist_ok=True)
            Path(target_file).write_text(skill_md, encoding="utf-8")

        logger.info(
            f"Forked skill '{source_name}' to '{target_name}' at {target_file} "
            f"(version {new_version})"
        )
        return target_file

    async def publish_skill(
        self,
        name: str,
        source_tier: str = "agent",
        target_tier: str = "tenant",
    ) -> str:
        """Publish a skill to a wider audience (e.g., agent -> tenant).

        Copies the skill from source tier to target tier with updated metadata.

        Args:
            name: Skill name to publish
            source_tier: Source tier (default: agent)
            target_tier: Target tier (default: tenant)

        Returns:
            Path to published SKILL.md file

        Raises:
            SkillNotFoundError: If skill not found in source tier
            SkillManagerError: If publish fails

        Example:
            >>> # Publish agent skill to tenant library
            >>> path = await manager.publish_skill("my-skill")
            >>>
            >>> # Publish tenant skill to system library
            >>> path = await manager.publish_skill("shared-skill", "tenant", "system")
        """
        # Validate tiers
        if source_tier not in SkillRegistry.TIER_PATHS:
            raise SkillManagerError(
                f"Invalid source tier '{source_tier}'. "
                f"Must be one of: {list(SkillRegistry.TIER_PATHS.keys())}"
            )

        if target_tier not in SkillRegistry.TIER_PATHS:
            raise SkillManagerError(
                f"Invalid target tier '{target_tier}'. "
                f"Must be one of: {list(SkillRegistry.TIER_PATHS.keys())}"
            )

        # Load source skill
        # First ensure registry has discovered the source tier
        await self._registry.discover(tiers=[source_tier])

        try:
            source_skill = await self._registry.get_skill(name)
        except SkillNotFoundError as e:
            raise SkillManagerError(f"Skill '{name}' not found in tier '{source_tier}'") from e

        # Verify the skill is actually in the source tier
        if source_skill.metadata.tier != source_tier:
            raise SkillManagerError(
                f"Skill '{name}' is in tier '{source_skill.metadata.tier}', not '{source_tier}'"
            )

        # Get target path
        target_tier_path = SkillRegistry.TIER_PATHS[target_tier]
        target_dir = f"{target_tier_path}{name}/"
        target_file = f"{target_dir}SKILL.md"

        # Update metadata for publication
        now = datetime.utcnow()

        import yaml

        frontmatter: dict[str, Any] = {
            "name": source_skill.metadata.name,
            "description": source_skill.metadata.description,
        }

        if source_skill.metadata.version:
            frontmatter["version"] = source_skill.metadata.version

        if source_skill.metadata.author:
            frontmatter["author"] = source_skill.metadata.author

        if source_skill.metadata.requires:
            frontmatter["requires"] = source_skill.metadata.requires

        # Track publication
        frontmatter["published_from"] = source_tier
        frontmatter["published_at"] = now.isoformat()

        # Preserve creation date, update modified date
        if source_skill.metadata.created_at:
            frontmatter["created_at"] = source_skill.metadata.created_at.isoformat()
        frontmatter["modified_at"] = now.isoformat()

        # Preserve additional metadata
        frontmatter.update(source_skill.metadata.metadata)

        frontmatter_yaml = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False)
        skill_md = f"---\n{frontmatter_yaml}---\n\n{source_skill.content}"

        # Write published skill
        if self._filesystem:
            # Create directory
            with contextlib.suppress(Exception):
                self._filesystem.mkdir(target_dir, parents=True)

            # Write file (CAS deduplication will happen automatically)
            self._filesystem.write(target_file, skill_md.encode("utf-8"))
        else:
            # Use local filesystem
            local_dir = Path(target_dir)
            local_dir.mkdir(parents=True, exist_ok=True)
            Path(target_file).write_text(skill_md, encoding="utf-8")

        logger.info(
            f"Published skill '{name}' from '{source_tier}' to '{target_tier}' at {target_file}"
        )
        return target_file

    async def search_skills(
        self,
        query: str,
        tier: str | None = None,
        limit: int | None = 10,
    ) -> list[tuple[str, float]]:
        """Search skills by description using text matching.

        This provides a simple text-based search across skill descriptions.
        For more advanced semantic search, use the Nexus semantic_search API.

        Args:
            query: Search query string
            tier: Optional tier to filter by (agent, tenant, system)
            limit: Maximum number of results (default: 10)

        Returns:
            List of (skill_name, score) tuples sorted by relevance

        Example:
            >>> # Search for code analysis skills
            >>> results = await manager.search_skills("code analysis")
            >>> for skill_name, score in results:
            ...     print(f"{skill_name}: {score:.2f}")
            >>>
            >>> # Search only in tenant skills
            >>> results = await manager.search_skills("data processing", tier="tenant")
        """
        # Ensure registry has discovered skills
        if not self._registry._metadata_index:
            await self._registry.discover()

        query_lower = query.lower()
        query_terms = query_lower.split()

        # Score skills by relevance
        scores: list[tuple[str, float]] = []

        # Get metadata list (guaranteed to be SkillMetadata with include_metadata=True)
        metadata_list_raw = self._registry.list_skills(tier=tier, include_metadata=True)
        metadata_list: list[SkillMetadata] = metadata_list_raw  # type: ignore[assignment]

        for metadata in metadata_list:
            if not metadata.description:
                continue

            # Simple scoring: count matching terms in description and name
            description_lower = metadata.description.lower()
            name_lower = metadata.name.lower()

            score = 0.0

            # Exact phrase match in description (highest score)
            if query_lower in description_lower:
                score += 10.0

            # Exact phrase match in name
            if query_lower in name_lower:
                score += 5.0

            # Count individual term matches
            for term in query_terms:
                if term in description_lower:
                    score += 2.0
                if term in name_lower:
                    score += 1.0

            if score > 0:
                scores.append((metadata.name, score))

        # Sort by score descending
        scores.sort(key=lambda x: x[1], reverse=True)

        # Limit results
        if limit:
            scores = scores[:limit]

        logger.debug(f"Search for '{query}' returned {len(scores)} results")
        return scores
