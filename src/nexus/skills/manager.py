"""Skill lifecycle management: create, fork, publish, and versioning."""

import contextlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nexus.core.exceptions import PermissionDeniedError, ValidationError
from nexus.core.permissions import OperationContext
from nexus.skills.models import SkillMetadata
from nexus.skills.parser import SkillParser
from nexus.skills.protocols import NexusFilesystem
from nexus.skills.registry import SkillNotFoundError, SkillRegistry

if TYPE_CHECKING:
    from nexus.core.rebac_manager import ReBACManager
    from nexus.skills.governance import SkillGovernance

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
        rebac_manager: "ReBACManager | None" = None,
        governance: "SkillGovernance | None" = None,
    ):
        """Initialize skill manager.

        Args:
            filesystem: Optional filesystem instance (defaults to local FS)
            registry: Optional skill registry for loading existing skills
            rebac_manager: Optional ReBAC manager for permission checks
            governance: Optional governance system for approval checks
        """
        self._filesystem = filesystem
        self._registry = registry or SkillRegistry(filesystem)
        self._parser = SkillParser()
        self._rebac = rebac_manager
        self._governance = governance

    async def _check_permission(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_name: str,
        tenant_id: str | None = None,
    ) -> bool:
        """Check if subject has permission on skill object.

        Args:
            subject_type: Type of subject (agent, user)
            subject_id: ID of subject
            permission: Permission to check (read, write, publish, etc.)
            object_name: Skill name
            tenant_id: Optional tenant ID for scoping

        Returns:
            True if permission granted, False otherwise
        """
        if not self._rebac:
            # No ReBAC manager - allow all operations (backward compatibility)
            return True

        try:
            return self._rebac.rebac_check(
                subject=(subject_type, subject_id),
                permission=permission,
                object=("skill", object_name),
                tenant_id=tenant_id,
            )
        except Exception as e:
            logger.warning(f"ReBAC check failed: {e}")
            return False

    async def _create_skill_permissions(
        self,
        skill_path: str,
        skill_dir: str,
        owner_type: str,
        owner_id: str,
        tier: str,
        tenant_id: str | None = None,
        context: OperationContext | None = None,
    ) -> None:
        """Create ReBAC permission tuples for a skill based on tier.

        Permission model:
        - user tier: Owner (user) gets direct_owner on the skill directory
        - tenant tier: All tenant members get viewer access (via tenant#member)
        - system tier: Everyone gets viewer access (via role#public)

        Args:
            skill_path: Full path to SKILL.md file
            skill_dir: Path to skill directory
            owner_type: Type of owner (agent, user)
            owner_id: ID of owner
            tier: Skill tier (agent, user, tenant, system)
            tenant_id: Optional tenant ID
            context: Operation context for additional info
        """
        if not self._rebac:
            # No ReBAC manager - skip permission creation
            return

        try:
            # Get tenant_id from context if not provided
            effective_tenant_id = tenant_id or (context.tenant_id if context else None) or "default"

            # For user-level skills: Owner gets direct_owner on the skill directory
            # This allows them full control (read, write, delete)
            if tier in ("user", "agent"):
                self._rebac.rebac_write(
                    subject=(owner_type, owner_id),
                    relation="direct_owner",
                    object=("file", skill_dir.rstrip("/")),
                    tenant_id=effective_tenant_id,
                )
                logger.debug(f"Created owner permission for {owner_type}:{owner_id} on {skill_dir}")

            # For tenant-level skills: Grant viewer access to all tenant members
            elif tier == "tenant":
                # Grant viewer access to the tenant (all members inherit read access)
                self._rebac.rebac_write(
                    subject=("tenant", effective_tenant_id),
                    relation="viewer",
                    object=("file", skill_dir.rstrip("/")),
                    tenant_id=effective_tenant_id,
                )
                logger.debug(
                    f"Created tenant viewer permission for tenant:{effective_tenant_id} on {skill_dir}"
                )

            # For system-level skills: Grant viewer access to everyone
            elif tier == "system":
                # Use "role#public" to grant access to everyone
                self._rebac.rebac_write(
                    subject=("role", "public"),
                    relation="viewer",
                    object=("file", skill_dir.rstrip("/")),
                    tenant_id="default",  # System skills use default tenant
                )
                logger.debug(f"Created public viewer permission on {skill_dir}")

            logger.info(f"Created ReBAC permissions for skill at {skill_path} (tier={tier})")

        except Exception as e:
            logger.error(f"Failed to create ReBAC permissions for skill at {skill_path}: {e}")
            # Don't fail the operation if ReBAC fails
            # The skill file is already created

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
        tenant_id: str | None = None,
        context: OperationContext | None = None,
        **kwargs: str,
    ) -> str:
        """Create a new skill from a template.

        Args:
            name: Skill name (alphanumeric with - or _)
            description: Skill description
            template: Template name (basic, data-analysis, code-generation, etc.)
            tier: Target tier (agent, user, tenant, system)
            author: Optional author name
            version: Initial version (default: 1.0.0)
            creator_id: ID of the creating agent/user (for ReBAC)
            creator_type: Type of creator (agent, user) - default: agent
            tenant_id: Tenant ID for scoping (for ReBAC)
            context: Operation context with user_id, tenant_id for path resolution
            **kwargs: Additional template variables

        Returns:
            Path to created SKILL.md file

        Raises:
            SkillManagerError: If creation fails
            PermissionDeniedError: If creator lacks permission

        Example:
            >>> path = await manager.create_skill(
            ...     "my-analyzer",
            ...     description="Analyzes code quality",
            ...     template="code-generation",
            ...     author="Alice",
            ...     context=context,
            ... )
        """
        # Validate skill name
        if not name.replace("-", "").replace("_", "").isalnum():
            raise SkillManagerError(f"Skill name must be alphanumeric (with - or _), got '{name}'")

        # Get context-aware tier paths
        tier_paths = SkillRegistry.get_tier_paths(context)

        # Validate tier
        if tier not in tier_paths:
            # Fall back to static paths for backward compatibility
            if tier not in SkillRegistry.TIER_PATHS:
                raise SkillManagerError(
                    f"Invalid tier '{tier}'. Must be one of: {list(SkillRegistry.TIER_PATHS.keys())}"
                )
            tier_paths = SkillRegistry.TIER_PATHS

        # Permission check: System tier requires admin (simplified for now)
        if tier == "system" and self._rebac and creator_id:
            logger.info(f"Creating system skill '{name}' by {creator_type}:{creator_id}")

        # Get tier path (context-aware)
        tier_path = tier_paths[tier]

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
        now = datetime.now(UTC)
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

        # Create ReBAC permissions for the skill based on tier
        # Even without creator_id, we need to set permissions for tenant/system skills
        owner_type = creator_type
        owner_id = (
            creator_id
            or (context.user_id if context else None)
            or (context.user if context else None)
            or "anonymous"
        )
        await self._create_skill_permissions(
            skill_path=skill_file,
            skill_dir=skill_dir,
            owner_type=owner_type,
            owner_id=owner_id,
            tier=tier,
            tenant_id=tenant_id,
            context=context,
        )

        logger.info(f"Created skill '{name}' from template '{template}' at {skill_file}")
        return skill_file

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
        """Create a new skill from content (e.g., from web scraping).

        Args:
            name: Skill name (alphanumeric with - or _)
            description: Skill description
            content: Skill content (markdown)
            tier: Target tier (agent, user, tenant, system)
            author: Optional author name
            version: Initial version (default: 1.0.0)
            source_url: Optional source URL (for tracking origin)
            metadata: Optional additional metadata
            context: Operation context with user_id, tenant_id for path resolution

        Returns:
            Path to created SKILL.md file

        Raises:
            SkillManagerError: If creation fails

        Example:
            >>> path = await manager.create_skill_from_content(
            ...     "stripe-api",
            ...     description="Stripe API Documentation",
            ...     content="# Stripe API\\n\\n...",
            ...     source_url="https://docs.stripe.com/api",
            ...     author="Auto-generated",
            ...     context=context,
            ... )
        """
        # Validate skill name
        if not name.replace("-", "").replace("_", "").isalnum():
            raise SkillManagerError(f"Skill name must be alphanumeric (with - or _), got '{name}'")

        # Get context-aware tier paths
        tier_paths = SkillRegistry.get_tier_paths(context)

        # Validate tier
        if tier not in tier_paths:
            # Fall back to static paths for backward compatibility
            if tier not in SkillRegistry.TIER_PATHS:
                raise SkillManagerError(
                    f"Invalid tier '{tier}'. Must be one of: {list(SkillRegistry.TIER_PATHS.keys())}"
                )
            tier_paths = SkillRegistry.TIER_PATHS

        # Get tier path (context-aware)
        tier_path = tier_paths[tier]

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

        # Create skill metadata
        now = datetime.now(UTC)

        # Generate SKILL.md content
        import yaml

        frontmatter: dict[str, Any] = {
            "name": name,
            "description": description,
            "version": version,
        }

        if author:
            frontmatter["author"] = author

        if source_url:
            frontmatter["source_url"] = source_url

        frontmatter["created_at"] = now.isoformat()
        frontmatter["modified_at"] = now.isoformat()

        # Add additional metadata if provided
        if metadata:
            frontmatter.update(metadata)

        frontmatter_yaml = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False)
        skill_md = f"---\n{frontmatter_yaml}---\n\n{content}"

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

        # Create ReBAC permissions for the skill based on tier
        owner_type = "user"
        owner_id = (
            (context.user_id if context else None)
            or (context.user if context else None)
            or "anonymous"
        )
        await self._create_skill_permissions(
            skill_path=skill_file,
            skill_dir=skill_dir,
            owner_type=owner_type,
            owner_id=owner_id,
            tier=tier,
            tenant_id=context.tenant_id if context else None,
            context=context,
        )

        logger.info(f"Created skill '{name}' from content at {skill_file}")
        return skill_file

    async def fork_skill(
        self,
        source_name: str,
        target_name: str,
        tier: str = "user",
        author: str | None = None,
        creator_id: str | None = None,
        creator_type: str = "user",
        tenant_id: str | None = None,
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
            creator_id: ID of the creating agent/user (for ReBAC)
            creator_type: Type of creator (agent, user) - default: agent
            tenant_id: Tenant ID for scoping (for ReBAC)

        Returns:
            Path to forked SKILL.md file

        Raises:
            SkillNotFoundError: If source skill not found
            SkillManagerError: If fork fails
            PermissionDeniedError: If creator lacks read permission on source

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

        # Check read permission on source skill
        if creator_id and not await self._check_permission(
            creator_type, creator_id, "fork", source_name, tenant_id
        ):
            raise PermissionDeniedError(
                f"No permission to fork skill '{source_name}'. "
                f"Subject ({creator_type}:{creator_id}) lacks 'fork' permission."
            )

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
        now = datetime.now(UTC)

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

        # Create ReBAC permissions for the forked skill
        owner_id = creator_id or "anonymous"
        await self._create_skill_permissions(
            skill_path=target_file,
            skill_dir=target_dir,
            owner_type=creator_type,
            owner_id=owner_id,
            tier=tier,
            tenant_id=tenant_id,
            context=None,  # fork_skill doesn't take context parameter
        )

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
        publisher_id: str | None = None,
        publisher_type: str = "agent",
        tenant_id: str | None = None,
    ) -> str:
        """Publish a skill to a wider audience (e.g., agent -> tenant).

        Copies the skill from source tier to target tier with updated metadata.

        Args:
            name: Skill name to publish
            source_tier: Source tier (default: agent)
            target_tier: Target tier (default: tenant)
            publisher_id: ID of the publishing agent/user (for ReBAC)
            publisher_type: Type of publisher (agent, user) - default: agent
            tenant_id: Tenant ID for scoping (for ReBAC)

        Returns:
            Path to published SKILL.md file

        Raises:
            SkillNotFoundError: If skill not found in source tier
            SkillManagerError: If publish fails
            PermissionDeniedError: If publisher lacks publish permission

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

        # Check publish permission
        if publisher_id and not await self._check_permission(
            publisher_type, publisher_id, "publish", name, tenant_id
        ):
            raise PermissionDeniedError(
                f"No permission to publish skill '{name}'. "
                f"Subject ({publisher_type}:{publisher_id}) lacks 'publish' permission."
            )

        # Check approval status (governance requirement)
        if self._governance:
            is_approved = await self._governance.is_approved(name)
            if not is_approved:
                from nexus.skills.governance import GovernanceError

                raise GovernanceError(
                    f"Skill '{name}' must be approved before publication. "
                    f"Submit for approval with: nexus skills submit-approval {name}"
                )

        # Get target path
        target_tier_path = SkillRegistry.TIER_PATHS[target_tier]
        target_dir = f"{target_tier_path}{name}/"
        target_file = f"{target_dir}SKILL.md"

        # Update metadata for publication
        now = datetime.now(UTC)

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

        # Update ReBAC permissions for the published skill
        # When publishing to a different tier, update the tenant association
        if self._rebac and tenant_id:
            try:
                # For system tier, add public access
                if target_tier == "system":
                    self._rebac.rebac_write(
                        subject=("*", "*"),
                        relation="public",
                        object=("skill", name),
                        tenant_id=None,  # System skills are global
                    )
                    logger.debug(f"Added public access for system skill '{name}'")

                # For tenant tier, update tenant association
                elif target_tier == "tenant":
                    self._rebac.rebac_write(
                        subject=("tenant", tenant_id),
                        relation="tenant",
                        object=("skill", name),
                        tenant_id=tenant_id,
                    )
                    logger.debug(f"Updated tenant association for skill '{name}'")

            except Exception as e:
                logger.warning(
                    f"Failed to update ReBAC permissions for published skill '{name}': {e}"
                )
                # Don't fail the publish operation if ReBAC update fails

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
