"""Skill Service - Stateless business logic for skill operations.

This service implements the Mixin + Service architecture:
- All state lives in ReBAC (permissions), Filesystem (content), UserConfig (subscriptions)
- Service is stateless - just orchestrates operations between components

## Permission-Based APIs (RFC: skill-permission-based-refactor.md)

Distribution:
- share: Grant read permission on a skill
- unshare: Revoke read permission on a skill

Subscription:
- discover: List skills the user has permission to see
- subscribe: Add a skill to user's library
- unsubscribe: Remove a skill from user's library

Runner:
- get_prompt_context: Get skill metadata for system prompt injection
- load: Load full skill content on-demand
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any, cast

from nexus.core.exceptions import PermissionDeniedError, ValidationError
from nexus.skills.types import PromptContext, SkillContent, SkillInfo

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext
    from nexus.core.rebac_manager import ReBACManager
    from nexus.services.gateway import NexusFSGateway


class SkillService:
    """Stateless skill service with permission-based access control.

    Implements the Mixin + Service architecture where:
    - All state lives in ReBAC (permissions), Filesystem (content), UserConfig (subscriptions)
    - Service is stateless - just orchestrates operations between components

    APIs:
        - share/unshare: Grant/revoke read permissions via ReBAC
        - discover: Find skills user has permission to see
        - subscribe/unsubscribe: Manage user's skill library
        - get_prompt_context: Get metadata for agent system prompts
        - load: Load full skill content on-demand

    Example:
        ```python
        from nexus.services.gateway import NexusFSGateway

        gateway = NexusFSGateway(fs)
        skill_service = SkillService(gateway=gateway)

        # Share a skill with the zone
        skill_service.share(
            skill_path="/zone/acme/user/alice/skill/code-review/",
            share_with="zone",
            context=ctx,
        )

        # Discover available skills
        skills = skill_service.discover(context=ctx)

        # Subscribe to a skill
        skill_service.subscribe(
            skill_path="/zone/acme/user/bob/skill/testing/",
            context=ctx,
        )

        # Load full skill content for agent use
        content = skill_service.load(
            skill_path="/zone/acme/user/alice/skill/code-review/",
            context=ctx,
        )
        ```
    """

    def __init__(
        self,
        gateway: NexusFSGateway,
    ):
        """Initialize skill service.

        Args:
            gateway: NexusFSGateway for filesystem and ReBAC operations
        """
        self._gw = gateway

        logger.info("[SkillService] Initialized")

    # =========================================================================
    # Distribution APIs
    # =========================================================================

    def share(
        self,
        skill_path: str,
        share_with: str,
        context: OperationContext | None,
    ) -> str:
        """Grant read permission on a skill to users, groups, or make public.

        Only the skill owner can share a skill. Uses ReBAC to manage permissions.
        The skill content stays at its original location - only permissions change.

        Args:
            skill_path: Full path to skill (e.g., /zone/acme/user/alice/skill/code-review/)
            share_with: Target to share with:
                - "public" - Make visible to everyone
                - "zone" - Share with all users in current zone
                - "group:<name>" - Share with a group
                - "user:<id>" - Share with specific user
                - "agent:<id>" - Share with specific agent
            context: Operation context with user_id and zone_id

        Returns:
            Tuple ID of the created permission

        Raises:
            ValidationError: If context is missing or share_with format is invalid
            PermissionDeniedError: If caller doesn't own the skill
        """
        self._validate_context(context)
        assert context is not None  # Validated by _validate_context
        self._assert_skill_owner(skill_path, context)

        # Parse share_with and build subject tuple
        share_subject = self._parse_share_target(share_with, context)

        # Create direct_viewer permission via ReBAC (use NexusFS method)
        # NOTE: Use "direct_viewer" (not "viewer") because "viewer" is a computed union relation.
        # Direct grants must use direct_* relations to be recognized by the permission system.
        # Normalize path to remove trailing slash - must match hierarchy_manager's parent tuple format
        # for permission inheritance to work correctly.
        normalized_path = skill_path.rstrip("/")
        result = self._gw._fs.rebac_create(
            subject=cast(Any, share_subject),  # 3-tuple for userset-as-subject
            relation="direct_viewer",
            object=("file", normalized_path),
            context=context,
        )

        logger.info(f"Shared skill '{skill_path}' with '{share_with}'")
        # rebac_create returns a dict with tuple_id, revision, consistency_token
        # Extract just the tuple_id string for the API contract
        return cast(str, result["tuple_id"])

    def unshare(
        self,
        skill_path: str,
        unshare_from: str,
        context: OperationContext | None,
    ) -> bool:
        """Revoke read permission on a skill from a user, group, or public.

        Only the skill owner can revoke sharing permissions.

        Args:
            skill_path: Full path to skill
            unshare_from: Target to unshare from (same format as share_with)
            context: Operation context with user_id and zone_id

        Returns:
            True if permission was revoked, False if not found

        Raises:
            ValidationError: If context is missing or format is invalid
            PermissionDeniedError: If caller doesn't own the skill
        """
        self._validate_context(context)
        assert context is not None  # Validated by _validate_context
        self._assert_skill_owner(skill_path, context)

        # Parse target and find matching tuple
        share_subject = self._parse_share_target(unshare_from, context)

        # Search for the tuple (use NexusFS method)
        if len(share_subject) == 3:
            search_subject = (share_subject[0], share_subject[1])
        else:
            search_subject = share_subject

        # Normalize path to match share() - must strip trailing slash
        normalized_path = skill_path.rstrip("/")
        tuples = self._gw._fs.rebac_list_tuples(
            subject=search_subject,
            relation="direct_viewer",
            object=("file", normalized_path),
        )

        if not tuples:
            logger.warning(f"No share found for skill '{skill_path}' to '{unshare_from}'")
            return False

        # Delete the first matching tuple (use raw manager)
        rebac = self._get_rebac()
        rebac.rebac_delete(tuples[0]["tuple_id"])
        logger.info(f"Unshared skill '{skill_path}' from '{unshare_from}'")
        return True

    # =========================================================================
    # Subscription APIs
    # =========================================================================

    def discover(
        self,
        context: OperationContext | None,
        filter: str = "all",
    ) -> list[SkillInfo]:
        """Discover skills the user has permission to see.

        Returns skills from all tiers that the user has read permission on.
        Also indicates which skills are in the user's subscribed library.

        Args:
            context: Operation context with user_id and zone_id
            filter: Filter mode:
                - "all" - All skills user can see
                - "public" - Only public skills
                - "shared" - Only skills shared directly with user (not public, not owned)
                - "zone" - Only zone-shared skills
                - "subscribed" - Only skills in user's library
                - "owned" - Only skills owned by user

        Returns:
            List of SkillInfo objects
        """
        self._validate_context(context)
        assert context is not None  # Validated by _validate_context
        logger.info(
            f"[discover] START filter={filter}, context.user_id={context.user_id}, context.zone_id={context.zone_id}"
        )

        rebac = self._get_rebac()
        subscribed_skills = set(self._load_subscriptions(context))

        # For "subscribed" filter, directly use subscribed skill paths
        if filter == "subscribed":
            logger.info(f"[discover] Returning subscribed skills directly: {subscribed_skills}")
            results: list[SkillInfo] = []
            for path in subscribed_skills:
                is_public = self._is_skill_public(path)
                metadata = self._load_skill_metadata(path, context, is_public=is_public)
                results.append(
                    SkillInfo(
                        path=path,
                        name=metadata.get("name", path.rstrip("/").split("/")[-1]),
                        description=metadata.get("description", ""),
                        owner=self._extract_owner_from_path(path),
                        is_subscribed=True,
                        is_public=is_public,
                        version=metadata.get("version"),
                        tags=metadata.get("tags", []),
                    )
                )
            return results

        # For "owned" filter, directly scan user's skill directory
        if filter == "owned":
            user_skill_dir = f"/zone/{context.zone_id}/user/{context.user_id}/skill/"
            owned_paths = self._find_skills_in_directory(user_skill_dir, context)
            logger.info(f"[discover] Returning owned skills directly: {owned_paths}")
            results = []
            for path in owned_paths:
                is_public = self._is_skill_public(path)
                metadata = self._load_skill_metadata(path, context, is_public=is_public)
                results.append(
                    SkillInfo(
                        path=path,
                        name=metadata.get("name", path.rstrip("/").split("/")[-1]),
                        description=metadata.get("description", ""),
                        owner=self._extract_owner_from_path(path),
                        is_subscribed=path in subscribed_skills,
                        is_public=is_public,
                        version=metadata.get("version"),
                        tags=metadata.get("tags", []),
                    )
                )
            return results

        # For "shared" filter, directly use shared skill paths
        if filter == "shared":
            shared_skill_paths = self._find_direct_viewer_skills(context)
            logger.info(f"[discover] Returning shared skills directly: {shared_skill_paths}")
            results = []
            for path in shared_skill_paths:
                is_public = self._is_skill_public(path)
                # Skip public skills - they're not "shared" per se
                if is_public:
                    continue
                metadata = self._load_skill_metadata(path, context, is_public=is_public)
                results.append(
                    SkillInfo(
                        path=path,
                        name=metadata.get("name", path.rstrip("/").split("/")[-1]),
                        description=metadata.get("description", ""),
                        owner=self._extract_owner_from_path(path),
                        is_subscribed=path in subscribed_skills,
                        is_public=is_public,
                        version=metadata.get("version"),
                        tags=metadata.get("tags", []),
                    )
                )
            return results

        # For "public" filter, directly use public skill paths
        if filter == "public":
            public_skill_paths = self._find_public_skills()
            logger.info(f"[discover] Returning public skills directly: {public_skill_paths}")
            results = []
            for path in public_skill_paths:
                metadata = self._load_skill_metadata(path, context, is_public=True)
                results.append(
                    SkillInfo(
                        path=path,
                        name=metadata.get("name", path.rstrip("/").split("/")[-1]),
                        description=metadata.get("description", ""),
                        owner=self._extract_owner_from_path(path),
                        is_subscribed=path in subscribed_skills,
                        is_public=True,
                        version=metadata.get("version"),
                        tags=metadata.get("tags", []),
                    )
                )
            return results

        # TODO: Implement "zone" filter - skills shared with the zone
        # if filter == "zone":
        #     zone_skill_paths = self._find_zone_shared_skills(context)
        #     ...

        # Collect skill paths from filesystem for other filters
        skill_paths = self._collect_skill_paths(context)
        logger.info(f"[discover] Found {len(skill_paths)} skill paths: {skill_paths[:5]}")

        # Filter by permission and build results
        # user_id is validated by _validate_context
        assert context.user_id is not None
        subject: tuple[str, str] = ("user", context.user_id)
        results = []

        for path in skill_paths:
            # Check read permission (includes public check)
            can_read = self._can_read_skill(path, context)
            logger.info(f"[discover] path={path}, can_read={can_read}")
            if not can_read:
                continue

            # Check if public (for display purposes)
            is_public = self._is_skill_public(path)

            is_subscribed = path in subscribed_skills

            # Apply filter
            if filter == "public" and not is_public:
                continue
            if filter == "owned":
                # Check ownership on SKILL.md file (where ownership tuples exist)
                skill_md_path = f"{path.rstrip('/')}/SKILL.md"
                has_ownership = rebac.rebac_check(
                    subject=subject,
                    permission="owner",
                    object=("file", skill_md_path),
                    zone_id=context.zone_id,
                )
                if not has_ownership:
                    continue

            # Load metadata (use cache if available)
            # Pass is_public so we can read metadata for public skills from other zones
            metadata = self._load_skill_metadata(path, context, is_public=is_public)

            results.append(
                SkillInfo(
                    path=path,
                    name=metadata.get("name", path.rstrip("/").split("/")[-1]),
                    description=metadata.get("description", ""),
                    owner=self._extract_owner_from_path(path),
                    is_subscribed=is_subscribed,
                    is_public=is_public,
                    version=metadata.get("version"),
                    tags=metadata.get("tags", []),
                )
            )

        return results

    def subscribe(
        self,
        skill_path: str,
        context: OperationContext | None,
    ) -> bool:
        """Subscribe to a skill, adding it to the user's library.

        The user must have read permission on the skill to subscribe.

        Args:
            skill_path: Full path to skill
            context: Operation context with user_id and zone_id

        Returns:
            True if newly subscribed, False if already subscribed

        Raises:
            PermissionDeniedError: If user doesn't have read permission
        """
        self._validate_context(context)
        assert context is not None  # Validated by _validate_context
        self._assert_can_read(skill_path, context)

        subscriptions = self._load_subscriptions(context)

        if skill_path in subscriptions:
            return False  # Already subscribed

        subscriptions.append(skill_path)
        self._save_subscriptions(context, subscriptions)
        logger.info(f"User '{context.user_id}' subscribed to skill '{skill_path}'")
        return True

    def unsubscribe(
        self,
        skill_path: str,
        context: OperationContext | None,
    ) -> bool:
        """Unsubscribe from a skill, removing it from the user's library.

        Args:
            skill_path: Full path to skill
            context: Operation context with user_id and zone_id

        Returns:
            True if unsubscribed, False if was not subscribed
        """
        self._validate_context(context)
        assert context is not None  # Validated by _validate_context

        subscriptions = self._load_subscriptions(context)

        if skill_path not in subscriptions:
            return False  # Not subscribed

        subscriptions.remove(skill_path)
        self._save_subscriptions(context, subscriptions)
        logger.info(f"User '{context.user_id}' unsubscribed from skill '{skill_path}'")
        return True

    # =========================================================================
    # Runner APIs
    # =========================================================================

    def get_prompt_context(
        self,
        context: OperationContext | None,
        max_skills: int = 50,
    ) -> PromptContext:
        """Get skill metadata formatted for system prompt injection.

        Returns metadata for subscribed/assigned skills in a format suitable for
        agent system prompts. Uses progressive disclosure - only metadata,
        not full content.

        For users: reads from .subscribed.yaml
        For agents: reads assigned_skills from config.yaml metadata

        Args:
            context: Operation context with user_id and zone_id
            max_skills: Maximum number of skills to include

        Returns:
            PromptContext with XML-formatted skill list and metadata
        """
        self._validate_context(context)
        assert context is not None  # Validated by _validate_context

        # For agents: read assigned_skills from config.yaml
        # For users: read subscriptions from .subscribed.yaml
        if hasattr(context, "subject_type") and context.subject_type == "agent":
            subscribed_skills = self._load_assigned_skills(context)
        else:
            subscribed_skills = self._load_subscriptions(context)

        skills_for_prompt: list[SkillInfo] = []

        for skill_path in subscribed_skills[:max_skills]:
            # Check read permission (includes public check, works cross-zone)
            if not self._can_read_skill(skill_path, context):
                continue

            # Check if skill is public (for metadata loading)
            is_public = self._is_skill_public(skill_path)

            # Load metadata (with public flag for cross-zone public skills)
            metadata = self._load_skill_metadata(skill_path, context, is_public=is_public)

            skills_for_prompt.append(
                SkillInfo(
                    path=skill_path,
                    name=metadata.get("name", skill_path.rstrip("/").split("/")[-1]),
                    description=metadata.get("description", ""),
                    owner=self._extract_owner_from_path(skill_path),
                    version=metadata.get("version"),
                )
            )

        # Build XML format for system prompt
        xml_content = self._format_skills_xml(skills_for_prompt)
        token_estimate = len(xml_content) // 4  # ~4 chars per token

        return PromptContext(
            xml=xml_content,
            skills=skills_for_prompt,
            count=len(skills_for_prompt),
            token_estimate=token_estimate,
        )

    def load(
        self,
        skill_path: str,
        context: OperationContext | None,
    ) -> SkillContent:
        """Load full skill content on-demand.

        Called when an agent needs to use a skill. Loads complete SKILL.md
        content including all instructions and examples.

        Args:
            skill_path: Full path to skill
            context: Operation context with user_id and zone_id

        Returns:
            SkillContent with full markdown content and metadata

        Raises:
            PermissionDeniedError: If user doesn't have read permission
            ValidationError: If skill cannot be loaded
        """
        self._validate_context(context)
        assert context is not None  # Validated by _validate_context
        self._assert_can_read(skill_path, context)

        skill_md_path = f"{skill_path}SKILL.md"

        # Load from filesystem
        try:
            content = self._gw.read(skill_md_path, context=context)
            if isinstance(content, bytes):
                content = content.decode("utf-8")
            if not content:
                content = ""
        except Exception as e:
            raise ValidationError(f"Failed to read skill content: {e}") from e

        # Parse content
        metadata, body = self._parse_skill_content(content)

        return SkillContent(
            path=skill_path,
            name=metadata.get("name", skill_path.rstrip("/").split("/")[-1]),
            description=metadata.get("description", ""),
            owner=self._extract_owner_from_path(skill_path),
            content=body,
            metadata=metadata,
        )

    # =========================================================================
    # Helper Methods: Permission & Validation
    # =========================================================================

    def _validate_context(self, context: OperationContext | None) -> None:
        """Validate that context has required fields."""
        if not context or not context.zone_id or not context.user_id:
            raise ValidationError("Context with zone_id and user_id required")

    def _get_rebac(self) -> ReBACManager:
        """Get ReBAC manager instance from gateway."""
        # Gateway wraps NexusFS which has _rebac_manager
        if hasattr(self._gw, "_fs") and hasattr(self._gw._fs, "_rebac_manager"):
            mgr = self._gw._fs._rebac_manager
            if mgr is not None:
                return mgr
        raise RuntimeError("ReBAC manager not configured")

    def _extract_owner_from_path(self, skill_path: str) -> str:
        """Extract owner user_id from skill path.

        Path format: /zone/{zone_id}/user/{user_id}/skill/{skill_name}/

        Returns:
            User ID if found, otherwise "unknown"
        """
        import re

        match = re.search(r"/user/([^/]+)/skill/", skill_path)
        if match:
            return match.group(1)
        return "unknown"

    def _assert_skill_owner(self, skill_path: str, context: OperationContext) -> None:
        """Assert that the user owns the skill (has execute permission).

        Ownership is checked on the SKILL.md file because that's where
        ownership tuples are created when the skill is written.
        """
        rebac = self._get_rebac()
        # Check ownership on SKILL.md file (where ownership tuples exist)
        skill_md_path = f"{skill_path.rstrip('/')}/SKILL.md"
        # user_id is validated by caller via _validate_context
        assert context.user_id is not None
        has_ownership = rebac.rebac_check(
            subject=("user", context.user_id),
            permission="execute",
            object=("file", skill_md_path),
            zone_id=context.zone_id,
        )
        if not has_ownership:
            raise PermissionDeniedError(
                f"User '{context.user_id}' does not own skill '{skill_path}'"
            )

    def _assert_can_read(self, skill_path: str, context: OperationContext) -> None:
        """Assert that the user can read the skill.

        A user can read a skill if:
        1. They have direct read permission, OR
        2. The skill is public (role:public has viewer access)
        """
        if not self._can_read_skill(skill_path, context):
            raise PermissionDeniedError(
                f"User '{context.user_id}' cannot read skill '{skill_path}'"
            )

    def _can_read_skill(self, skill_path: str, context: OperationContext) -> bool:
        """Check if user can read skill (direct permission OR public).

        Permission is checked on SKILL.md file because:
        - Ownership tuples are created on the file, not directory
        - Parent inheritance goes parent→child, not child→parent

        Returns True if:
        1. User has direct read permission on SKILL.md, OR
        2. The skill is public (role:public has viewer on directory or SKILL.md)
        """
        rebac = self._get_rebac()
        skill_md_path = f"{skill_path.rstrip('/')}/SKILL.md"

        # Check direct user permission on SKILL.md
        # user_id is validated by caller via _validate_context
        assert context.user_id is not None
        has_direct_read = rebac.rebac_check(
            subject=("user", context.user_id),
            permission="read",
            object=("file", skill_md_path),
            zone_id=context.zone_id,
        )
        if has_direct_read:
            return True

        # Check if skill is public (on directory path where share tuple exists)
        # For public skills, we check without zone restriction
        # Normalize path to match share() - must strip trailing slash
        normalized_path = skill_path.rstrip("/")
        is_public = rebac.rebac_check(
            subject=("role", "public"),
            permission="read",
            object=("file", normalized_path),
            zone_id=None,  # Public skills are zone-agnostic
        )
        return is_public

    def _is_skill_public(self, skill_path: str) -> bool:
        """Check if a skill is publicly shared."""
        rebac = self._get_rebac()
        # Normalize path to match share() - must strip trailing slash
        normalized_path = skill_path.rstrip("/")
        result = rebac.rebac_check(
            subject=("role", "public"),
            permission="read",
            object=("file", normalized_path),
            zone_id=None,  # Public check is zone-agnostic
        )
        logger.info(
            f"[_is_skill_public] path={skill_path}, normalized={normalized_path}, result={result}"
        )
        return result

    def _parse_share_target(
        self, share_with: str, context: OperationContext
    ) -> tuple[str, str] | tuple[str, str, str]:
        """Parse share_with string into ReBAC subject tuple."""
        if share_with == "public":
            return ("role", "public")
        elif share_with == "zone":
            # zone_id is validated by caller via _validate_context
            assert context.zone_id is not None
            return ("zone", context.zone_id, "member")
        elif share_with.startswith("group:"):
            group_name = share_with[6:]
            if not group_name:
                raise ValidationError("Group name cannot be empty")
            return ("group", group_name, "member")
        elif share_with.startswith("user:"):
            user_id = share_with[5:]
            if not user_id:
                raise ValidationError("User ID cannot be empty")
            return ("user", user_id)
        elif share_with.startswith("agent:"):
            agent_id = share_with[6:]
            if not agent_id:
                raise ValidationError("Agent ID cannot be empty")
            return ("agent", agent_id)
        else:
            raise ValidationError(
                f"Invalid share_with format: '{share_with}'. "
                f"Expected: 'public', 'zone', 'group:<name>', 'user:<id>', or 'agent:<id>'"
            )

    # =========================================================================
    # Helper Methods: Subscriptions
    # =========================================================================

    def _get_subscriptions_path(self, context: OperationContext) -> str:
        """Get path to user's subscriptions config file."""
        return f"/zone/{context.zone_id}/user/{context.user_id}/skill/.subscribed.yaml"

    def _load_subscriptions(self, context: OperationContext) -> list[str]:
        """Load user's subscribed skills from config file."""
        import yaml

        path = self._get_subscriptions_path(context)

        try:
            content = self._gw.read(path, context=context)
            if isinstance(content, bytes):
                content = content.decode("utf-8")
            if content:
                data = yaml.safe_load(content)
                if data:
                    result: list[str] = data.get("subscribed_skills", [])
                    return result
        except Exception:
            pass  # File doesn't exist or invalid

        return []

    def _load_assigned_skills(self, context: OperationContext) -> list[str]:
        """Load agent's assigned skills from config.yaml metadata.

        For agents, skills are assigned (not subscribed) and stored in
        the agent's config.yaml under metadata.assigned_skills.

        Args:
            context: Operation context with subject_id as agent_id (format: user_id,agent_name)

        Returns:
            List of assigned skill paths
        """
        import yaml

        # Extract agent_id from context (format: user_id,agent_name)
        # For agent contexts: agent_id or subject_id contains the full agent_id
        agent_id = context.agent_id or context.subject_id

        if not agent_id or "," not in str(agent_id):
            logger.warning(f"Invalid agent_id format: {agent_id}")
            return []

        # Parse agent_id to get user_id and agent_name
        user_id, agent_name = agent_id.split(",", 1)

        # Build path to agent's config.yaml
        config_path = f"/zone/{context.zone_id}/user/{user_id}/agent/{agent_name}/config.yaml"

        try:
            content = self._gw.read(config_path, context=context)
            if isinstance(content, bytes):
                content = content.decode("utf-8")
            if content:
                data = yaml.safe_load(content)
                if data and isinstance(data, dict):
                    # Get assigned_skills from metadata
                    metadata = data.get("metadata", {})
                    if isinstance(metadata, dict):
                        assigned_skills: list[str] = metadata.get("assigned_skills", [])
                        logger.info(
                            f"Loaded {len(assigned_skills)} assigned skills for agent {agent_id}"
                        )
                        return assigned_skills
        except Exception as e:
            logger.warning(f"Failed to load assigned skills for agent {agent_id}: {e}")

        return []

    def _save_subscriptions(self, context: OperationContext, skills: list[str]) -> None:
        """Save user's subscribed skills to config file."""
        import yaml

        path = self._get_subscriptions_path(context)

        data = {"subscribed_skills": skills}
        content = yaml.dump(data, default_flow_style=False, sort_keys=False)

        # Ensure parent directory exists
        parent_dir = "/".join(path.split("/")[:-1])
        with contextlib.suppress(Exception):
            self._gw.mkdir(parent_dir, context=context)

        self._gw.write(path, content, context=context)

    # =========================================================================
    # Helper Methods: Skill Discovery & Metadata
    # =========================================================================

    def _collect_skill_paths(self, context: OperationContext) -> list[str]:
        """Collect all skill paths from filesystem.

        Finds directories containing SKILL.md files.
        """
        skill_paths: list[str] = []

        def find_skills_in_dir(base_dir: str) -> None:
            """Find skill directories within a base directory."""
            try:
                if not self._gw.exists(base_dir, context=context):
                    return

                # List all items (returns full paths like /base/skill-name/SKILL.md)
                items = self._gw.list(base_dir, context=context)

                # Extract unique skill directories from paths
                seen_skills: set[str] = set()
                for item in items:
                    item_str = str(item)

                    # Skip if not under base_dir
                    if not item_str.startswith(base_dir):
                        continue

                    # Get the relative path after base_dir
                    relative = item_str[len(base_dir) :]

                    # Extract skill directory name (first path component)
                    skill_name = relative.split("/")[0] if "/" in relative else relative

                    # Skip if empty or a file at root level
                    if not skill_name or skill_name.endswith((".md", ".json")):
                        continue

                    if skill_name not in seen_skills:
                        skill_path = f"{base_dir}{skill_name}/"
                        # Verify this is a skill by checking for SKILL.md
                        skill_md = f"{skill_path}SKILL.md"
                        try:
                            if self._gw.exists(skill_md, context=context):
                                skill_paths.append(skill_path)
                                seen_skills.add(skill_name)
                        except Exception:
                            pass
            except Exception:
                pass

        # System skills
        find_skills_in_dir("/skill/")

        # Zone skills
        find_skills_in_dir(f"/zone/{context.zone_id}/skill/")

        # User skills
        find_skills_in_dir(f"/zone/{context.zone_id}/user/{context.user_id}/skill/")

        # Cross-zone public skills
        # Find all skills shared with role:public from any zone
        public_skill_paths = self._find_public_skills()
        for path in public_skill_paths:
            if path not in skill_paths:
                skill_paths.append(path)

        # Skills shared directly with the user via direct_viewer
        shared_skill_paths = self._find_direct_viewer_skills(context)
        for path in shared_skill_paths:
            if path not in skill_paths:
                skill_paths.append(path)

        return skill_paths

    def _find_public_skills(self) -> list[str]:
        """Find all publicly shared skills across all zones.

        Queries the ReBAC tuples to find all skills that have been
        shared with (role, public) as viewer.

        Returns:
            List of skill paths that are publicly shared
        """
        public_paths: list[str] = []

        try:
            # Query rebac_list_tuples for public direct_viewer tuples on skill directories
            tuples = self._gw._fs.rebac_list_tuples(
                subject=("role", "public"),
                relation="direct_viewer",
            )

            for t in tuples:
                obj_type = t.get("object_type")
                obj_id = t.get("object_id")

                # Only include file objects that look like skill paths
                # Skill paths contain "/skill/" in them
                if obj_type == "file" and obj_id and "/skill/" in obj_id:
                    # Add trailing slash for consistency with directory convention
                    # (tuples are stored without trailing slash for hierarchy_manager compatibility)
                    skill_path = obj_id if obj_id.endswith("/") else obj_id + "/"
                    public_paths.append(skill_path)

        except Exception as e:
            logger.warning(f"Failed to find public skills: {e}")

        return public_paths

    def _find_skills_in_directory(self, base_dir: str, context: OperationContext) -> list[str]:
        """Find skill directories within a base directory.

        Args:
            base_dir: Base directory to scan (e.g., /zone/x/user/y/skill/)
            context: Operation context

        Returns:
            List of skill paths found in the directory
        """
        skill_paths: list[str] = []
        try:
            if not self._gw.exists(base_dir, context=context):
                return skill_paths

            # List all items in the directory
            items = self._gw.list(base_dir, context=context)

            # Extract unique skill directories
            seen_skills: set[str] = set()
            for item in items:
                item_str = str(item)

                # Skip if not under base_dir
                if not item_str.startswith(base_dir):
                    continue

                # Get the relative path after base_dir
                relative = item_str[len(base_dir) :]

                # Extract skill directory name (first path component)
                skill_name = relative.split("/")[0] if "/" in relative else relative

                # Skip if empty or a file at root level
                if (
                    not skill_name
                    or skill_name.startswith(".")
                    or skill_name.endswith((".md", ".json", ".yaml"))
                ):
                    continue

                if skill_name not in seen_skills:
                    skill_path = f"{base_dir}{skill_name}/"
                    # Verify this is a skill by checking for SKILL.md
                    skill_md = f"{skill_path}SKILL.md"
                    try:
                        if self._gw.exists(skill_md, context=context):
                            skill_paths.append(skill_path)
                            seen_skills.add(skill_name)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"Failed to find skills in {base_dir}: {e}")

        return skill_paths

    def _find_direct_viewer_skills(self, context: OperationContext) -> list[str]:
        """Find skills where user has direct_viewer relation.

        Queries the ReBAC tuples to find all skills that have been
        shared with the user via direct_viewer relation.

        Args:
            context: Operation context with user_id

        Returns:
            List of skill paths that are shared with the user
        """
        shared_paths: list[str] = []

        try:
            # Query rebac_list_tuples for direct_viewer tuples where user is the subject
            # user_id is validated by caller
            assert context.user_id is not None
            tuples = self._gw._fs.rebac_list_tuples(
                subject=("user", context.user_id),
                relation="direct_viewer",
            )

            for t in tuples:
                obj_type = t.get("object_type")
                obj_id = t.get("object_id")

                # Only include file objects that look like skill paths
                # Skill paths contain "/skill/" in them
                if obj_type == "file" and obj_id and "/skill/" in obj_id:
                    # Extract skill directory from SKILL.md path
                    if obj_id.endswith("/SKILL.md") or obj_id.endswith("SKILL.md"):
                        skill_path = obj_id[:-8]  # Remove "SKILL.md"
                    else:
                        skill_path = obj_id
                    # Add trailing slash for consistency
                    if not skill_path.endswith("/"):
                        skill_path = skill_path + "/"
                    shared_paths.append(skill_path)

        except Exception as e:
            logger.warning(f"Failed to find shared skills: {e}")

        return shared_paths

    def _load_skill_metadata(
        self, skill_path: str, context: OperationContext, *, is_public: bool = False
    ) -> dict[str, Any]:
        """Load skill metadata from SKILL.md file.

        Args:
            skill_path: Path to skill directory
            context: Operation context for permission checks
            is_public: If True, read with elevated access for public skills
        """
        skill_md_path = f"{skill_path}SKILL.md"

        try:
            content = self._gw.read(skill_md_path, context=context)
            if isinstance(content, bytes):
                content = content.decode("utf-8")
            if not content:
                return {}

            metadata, _ = self._parse_skill_content(content)
            return metadata

        except Exception:
            # If user context failed but skill is public, try with system access
            if is_public:
                try:
                    # Create a system context for reading public skill metadata
                    from nexus.core.permissions import OperationContext as OpCtx

                    system_ctx = OpCtx(
                        user="system",
                        groups=[],
                        zone_id=context.zone_id,
                        user_id="system",
                        is_system=True,
                    )
                    content = self._gw.read(skill_md_path, context=system_ctx)
                    if isinstance(content, bytes):
                        content = content.decode("utf-8")
                    if content:
                        metadata, _ = self._parse_skill_content(content)
                        return metadata
                except Exception as e:
                    logger.warning(f"Failed to read public skill metadata: {e}")
            return {}

    def _parse_skill_content(self, content: str) -> tuple[dict[str, Any], str]:
        """Parse SKILL.md content into metadata and body."""
        import re

        metadata: dict[str, Any] = {}
        body = content

        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                import yaml

                with contextlib.suppress(Exception):
                    metadata = yaml.safe_load(parts[1]) or {}
                body = parts[2].strip()
        else:
            # Fallback: parse first heading as name
            match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
            if match:
                metadata["name"] = match.group(1).strip()

        return metadata, body

    def _format_skills_xml(self, skills: list[SkillInfo]) -> str:
        """Format skills as XML for system prompt injection."""
        xml_parts = ["<available_skills>"]
        for skill in skills:
            xml_parts.append(f'  <skill path="{skill.path}">')
            xml_parts.append(f"    <name>{skill.name}</name>")
            xml_parts.append(f"    <description>{skill.description}</description>")
            xml_parts.append(f"    <owner>{skill.owner}</owner>")
            xml_parts.append("  </skill>")
        xml_parts.append("</available_skills>")
        return "\n".join(xml_parts)
