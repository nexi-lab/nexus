"""Skills Service — Distribution, Subscription, and Runner APIs.

Stateless business logic for skill operations. Uses protocol-based
dependencies (SkillFilesystemProtocol + SkillPermissionProtocol) instead of
the broad NexusFSGateway, enabling isolated testing with in-memory fakes.

Issue #2035: Extracted from nexus.services.skill_service into skills brick.

API Groups:
- Distribution: share / unshare skills
- Subscription: discover / subscribe / unsubscribe
- Runner: get_prompt_context / load skill content on-demand
"""

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from nexus.bricks.skills.exceptions import SkillPermissionDeniedError, SkillValidationError
from nexus.bricks.skills.types import PromptContext, SkillContent, SkillInfo
from nexus.services.protocols.rpc import rpc_expose

if TYPE_CHECKING:
    from nexus.services.protocols.skill_deps import (
        SkillFilesystemProtocol,
        SkillPermissionProtocol,
    )

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _SystemContext:
    """Minimal context for system-level skill operations (no core imports)."""

    user_id: str = "system"
    zone_id: str | None = None
    agent_id: str | None = None
    is_admin: bool = True
    is_system: bool = True
    groups: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# TTL-based cache entry (Issue #2035, Phase 5.2)
# ---------------------------------------------------------------------------
_DEFAULT_SUBSCRIPTION_TTL = 5.0  # seconds
_MAX_METADATA_CACHE_SIZE = 1000  # Evict when exceeded
_OWNER_RE = re.compile(r"/user/([^/]+)/skill/")


@dataclass
class _CacheEntry:
    """TTL-based cache entry for subscriptions."""

    data: list[str]
    expires_at: float


class SkillService:
    """Stateless skill service with permission-based access control.

    All state lives in ReBAC (permissions), Filesystem (content),
    UserConfig (subscriptions). Service is stateless — just orchestrates.
    """

    def __init__(
        self,
        fs: "SkillFilesystemProtocol",
        perms: "SkillPermissionProtocol",
        create_system_context: Callable[..., Any] | None = None,
    ):
        """Initialize skill service with narrow protocol dependencies.

        Args:
            fs: Filesystem operations (read, write, mkdir, list, exists)
            perms: Permission operations (rebac_check, rebac_create, etc.)
            create_system_context: Optional factory for system context
                (used for public skill metadata fallback). When None,
                falls back to lazy import of OperationContext.
        """
        self._fs = fs
        self._perms = perms
        self._create_system_context = create_system_context

        # TTL-based subscription cache (Issue #2035, Phase 5.2)
        # Guarded by _subscriptions_lock for thread-safety
        self._subscriptions_cache: dict[tuple[str, str], _CacheEntry] = {}
        self._subscriptions_lock = threading.Lock()

        # Request-scoped metadata cache (Issue #2035, Phase 5.1)
        # Guarded by _metadata_lock for thread-safety with ThreadPoolExecutor
        self._metadata_cache: dict[str, dict[str, Any]] = {}
        self._metadata_lock = threading.Lock()

        logger.info("[SkillService] Initialized with protocol dependencies")

    def clear_metadata_cache(self) -> None:
        """Clear request-scoped metadata cache. Call at request boundary."""
        with self._metadata_lock:
            self._metadata_cache.clear()

    # =========================================================================
    # Distribution APIs
    # =========================================================================

    def share(
        self,
        skill_path: str,
        share_with: str,
        context: Any | None,
    ) -> str:
        """Grant read permission on a skill to users, groups, or make public."""
        self._validate_context(context)
        assert context is not None
        self._assert_skill_owner(skill_path, context)

        share_subject = self._parse_share_target(share_with, context)
        normalized_path = skill_path.rstrip("/")
        result = self._perms.rebac_create(
            subject=cast(Any, share_subject),
            relation="direct_viewer",
            object=("file", normalized_path),
            context=context,
        )

        logger.info(f"Shared skill '{skill_path}' with '{share_with}'")
        if result is None:
            raise RuntimeError("rebac_create returned None")
        return cast(str, result["tuple_id"])

    def unshare(
        self,
        skill_path: str,
        unshare_from: str,
        context: Any | None,
    ) -> bool:
        """Revoke read permission on a skill from a user, group, or public."""
        self._validate_context(context)
        assert context is not None
        self._assert_skill_owner(skill_path, context)

        share_subject = self._parse_share_target(unshare_from, context)
        if len(share_subject) == 3:
            search_subject = (share_subject[0], share_subject[1])
        else:
            search_subject = share_subject

        normalized_path = skill_path.rstrip("/")
        tuples = self._perms.rebac_list_tuples(
            subject=search_subject,
            relation="direct_viewer",
            object=("file", normalized_path),
        )

        if not tuples:
            logger.warning(f"No share found for skill '{skill_path}' to '{unshare_from}'")
            return False

        rebac = self._get_rebac()
        rebac.rebac_delete(tuples[0]["tuple_id"])
        logger.info(f"Unshared skill '{skill_path}' from '{unshare_from}'")
        return True

    # =========================================================================
    # Subscription APIs
    # =========================================================================

    def discover(
        self,
        context: Any | None,
        filter: str = "all",
    ) -> list[SkillInfo]:
        """Discover skills the user has permission to see."""
        return self._discover_impl(context, filter)

    def _discover_impl(
        self,
        context: Any | None,
        filter: str = "all",
    ) -> list[SkillInfo]:
        """Synchronous implementation of discover (reused by export)."""
        self._validate_context(context)
        assert context is not None
        logger.info(
            f"[discover] START filter={filter}, context.user_id={context.user_id}, context.zone_id={context.zone_id}"
        )

        subscribed_skills = set(self._load_subscriptions(context))

        # Pre-compute public set once (batch ReBAC, avoids N individual calls)
        public_paths = self._find_public_skills()
        public_set = {p.rstrip("/") for p in public_paths}

        if filter == "subscribed":
            logger.info(f"[discover] Returning subscribed skills directly: {subscribed_skills}")
            results: list[SkillInfo] = []
            for path in subscribed_skills:
                is_public = path.rstrip("/") in public_set
                metadata = self._load_skill_metadata(path, context, is_public=is_public)
                results.append(
                    self._build_skill_info(path, metadata, is_subscribed=True, is_public=is_public)
                )
            return results

        if filter == "owned":
            user_skill_dir = f"/zone/{context.zone_id}/user/{context.user_id}/skill/"
            owned_paths = self._find_skills_in_directory(user_skill_dir, context)
            logger.info(f"[discover] Returning owned skills directly: {owned_paths}")
            results = []
            for path in owned_paths:
                is_public = path.rstrip("/") in public_set
                metadata = self._load_skill_metadata(path, context, is_public=is_public)
                results.append(
                    self._build_skill_info(
                        path, metadata, is_subscribed=path in subscribed_skills, is_public=is_public
                    )
                )
            return results

        if filter == "shared":
            shared_skill_paths = self._find_direct_viewer_skills(context)
            logger.info(f"[discover] Returning shared skills directly: {shared_skill_paths}")
            results = []
            for path in shared_skill_paths:
                is_public = path.rstrip("/") in public_set
                if is_public:
                    continue
                metadata = self._load_skill_metadata(path, context, is_public=is_public)
                results.append(
                    self._build_skill_info(
                        path, metadata, is_subscribed=path in subscribed_skills, is_public=is_public
                    )
                )
            return results

        if filter == "public":
            logger.info(f"[discover] Returning public skills directly: {public_paths}")
            results = []
            for path in public_paths:
                metadata = self._load_skill_metadata(path, context, is_public=True)
                results.append(
                    self._build_skill_info(
                        path, metadata, is_subscribed=path in subscribed_skills, is_public=True
                    )
                )
            return results

        # "all" filter: batch-collect paths and pre-compute permission sets
        shared_paths = self._find_direct_viewer_skills(context)
        shared_set = {p.rstrip("/") for p in shared_paths}

        skill_paths = self._collect_skill_paths(
            context, public_paths=public_paths, shared_paths=shared_paths
        )
        logger.info(f"[discover] Found {len(skill_paths)} skill paths: {skill_paths[:5]}")

        results = []
        for path in skill_paths:
            normalized = path.rstrip("/")
            is_public = normalized in public_set

            # skip_public_check=True: public_set already pre-computed above
            if (
                not is_public
                and normalized not in shared_set
                and not self._can_read_skill(path, context, skip_public_check=True)
            ):
                continue

            metadata = self._load_skill_metadata(path, context, is_public=is_public)
            results.append(
                self._build_skill_info(
                    path,
                    metadata,
                    is_subscribed=path in subscribed_skills,
                    is_public=is_public,
                )
            )

        return results

    def subscribe(
        self,
        skill_path: str,
        context: Any | None,
    ) -> bool:
        """Subscribe to a skill, adding it to the user's library."""
        self._validate_context(context)
        assert context is not None
        self._assert_can_read(skill_path, context)

        subscriptions = self._load_subscriptions(context)
        if skill_path in subscriptions:
            return False

        subscriptions.append(skill_path)
        self._save_subscriptions(context, subscriptions)
        logger.info(f"User '{context.user_id}' subscribed to skill '{skill_path}'")
        return True

    def unsubscribe(
        self,
        skill_path: str,
        context: Any | None,
    ) -> bool:
        """Unsubscribe from a skill, removing it from the user's library."""
        self._validate_context(context)
        assert context is not None

        subscriptions = self._load_subscriptions(context)
        if skill_path not in subscriptions:
            return False

        subscriptions.remove(skill_path)
        self._save_subscriptions(context, subscriptions)
        logger.info(f"User '{context.user_id}' unsubscribed from skill '{skill_path}'")
        return True

    # =========================================================================
    # Runner APIs
    # =========================================================================

    def get_prompt_context(
        self,
        context: Any | None,
        max_skills: int = 50,
    ) -> PromptContext:
        """Get skill metadata formatted for system prompt injection.

        Issue #2035 Phase 5.3: Pre-compute public_set and shared_set
        to reduce per-skill ReBAC calls from ~100 to ~3 batch calls.
        """
        self._validate_context(context)
        assert context is not None

        if hasattr(context, "subject_type") and context.subject_type == "agent":
            subscribed_skills = self._load_assigned_skills(context)
        else:
            subscribed_skills = self._load_subscriptions(context)

        # Batch pre-compute permission sets (Issue #2035, Phase 5.3)
        public_set = {p.rstrip("/") for p in self._find_public_skills()}
        shared_set = {p.rstrip("/") for p in self._find_direct_viewer_skills(context)}

        skills_for_prompt: list[SkillInfo] = []

        for skill_path in subscribed_skills[:max_skills]:
            normalized = skill_path.rstrip("/")
            is_public = normalized in public_set

            # Fast-path: public and shared skills are always readable
            if (
                not is_public
                and normalized not in shared_set
                and not self._can_read_skill(skill_path, context)
            ):
                continue

            metadata = self._load_skill_metadata(skill_path, context, is_public=is_public)
            skills_for_prompt.append(
                self._build_skill_info(skill_path, metadata, is_public=is_public)
            )

        xml_content = self._format_skills_xml(skills_for_prompt)
        token_estimate = len(xml_content) // 4

        return PromptContext(
            xml=xml_content,
            skills=skills_for_prompt,
            count=len(skills_for_prompt),
            token_estimate=token_estimate,
        )

    def load(
        self,
        skill_path: str,
        context: Any | None,
    ) -> SkillContent:
        """Load full skill content on-demand."""
        self._validate_context(context)
        assert context is not None
        self._assert_can_read(skill_path, context)

        skill_md_path = f"{skill_path}SKILL.md"

        try:
            content = self._fs.sys_read(skill_md_path, context=context)
            if isinstance(content, bytes):
                content = content.decode("utf-8")
            if not content:
                content = ""
        except Exception as e:
            raise SkillValidationError(f"Failed to read skill content: {e}") from e

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

    def _validate_context(self, context: Any | None) -> None:
        """Validate that context has required fields."""
        if not context or not context.zone_id or not context.user_id:
            raise SkillValidationError("Context with zone_id and user_id required")

    def _get_rebac(self) -> Any:
        """Get ReBAC manager instance from permissions protocol."""
        mgr = self._perms.rebac_manager
        if mgr is not None:
            return mgr
        raise RuntimeError("ReBAC manager not configured")

    @staticmethod
    def _extract_owner_from_path(skill_path: str) -> str:
        """Extract owner user_id from skill path."""
        match = _OWNER_RE.search(skill_path)
        if match:
            return match.group(1)
        return "unknown"

    def _assert_skill_owner(self, skill_path: str, context: Any) -> None:
        """Assert that the user owns the skill (has execute permission)."""
        rebac = self._get_rebac()
        skill_md_path = f"{skill_path.rstrip('/')}/SKILL.md"
        assert context.user_id is not None
        has_ownership = rebac.rebac_check(
            subject=("user", context.user_id),
            permission="execute",
            object=("file", skill_md_path),
            zone_id=context.zone_id,
        )
        if not has_ownership:
            raise SkillPermissionDeniedError(
                f"User '{context.user_id}' does not own skill '{skill_path}'"
            )

    def _assert_can_read(self, skill_path: str, context: Any) -> None:
        """Assert that the user can read the skill."""
        if not self._can_read_skill(skill_path, context):
            raise SkillPermissionDeniedError(
                f"User '{context.user_id}' cannot read skill '{skill_path}'"
            )

    def _can_read_skill(
        self, skill_path: str, context: Any, *, skip_public_check: bool = False
    ) -> bool:
        """Check if user can read skill (direct permission OR public).

        Args:
            skip_public_check: When True, skip the public ReBAC call (caller
                already checked via pre-computed public_set).
        """
        rebac = self._get_rebac()
        skill_md_path = f"{skill_path.rstrip('/')}/SKILL.md"

        assert context.user_id is not None
        has_direct_read = rebac.rebac_check(
            subject=("user", context.user_id),
            permission="read",
            object=("file", skill_md_path),
            zone_id=context.zone_id,
        )
        if has_direct_read:
            return True

        if skip_public_check:
            return False

        normalized_path = skill_path.rstrip("/")
        is_public = rebac.rebac_check(
            subject=("role", "public"),
            permission="read",
            object=("file", normalized_path),
            zone_id=None,
        )
        return bool(is_public)

    def _is_skill_public(self, skill_path: str) -> bool:
        """Check if a skill is publicly shared."""
        rebac = self._get_rebac()
        normalized_path = skill_path.rstrip("/")
        result = rebac.rebac_check(
            subject=("role", "public"),
            permission="read",
            object=("file", normalized_path),
            zone_id=None,
        )
        return bool(result)

    def _build_skill_info(
        self,
        path: str,
        metadata: dict[str, Any],
        *,
        is_subscribed: bool = False,
        is_public: bool = False,
    ) -> SkillInfo:
        """Build a SkillInfo from path and parsed metadata."""
        return SkillInfo(
            path=path,
            name=metadata.get("name", path.rstrip("/").split("/")[-1]),
            description=metadata.get("description", ""),
            owner=self._extract_owner_from_path(path),
            is_subscribed=is_subscribed,
            is_public=is_public,
            version=metadata.get("version"),
            tags=metadata.get("tags", []),
        )

    def _parse_share_target(
        self, share_with: str, context: Any
    ) -> tuple[str, str] | tuple[str, str, str]:
        """Parse share_with string into ReBAC subject tuple."""
        if share_with == "public":
            return ("role", "public")
        elif share_with == "zone":
            assert context.zone_id is not None
            return ("zone", context.zone_id, "member")
        elif share_with.startswith("group:"):
            group_name = share_with[6:]
            if not group_name:
                raise SkillValidationError("Group name cannot be empty")
            return ("group", group_name, "member")
        elif share_with.startswith("user:"):
            user_id = share_with[5:]
            if not user_id:
                raise SkillValidationError("User ID cannot be empty")
            return ("user", user_id)
        elif share_with.startswith("agent:"):
            agent_id = share_with[6:]
            if not agent_id:
                raise SkillValidationError("Agent ID cannot be empty")
            return ("agent", agent_id)
        else:
            raise SkillValidationError(
                f"Invalid share_with format: '{share_with}'. "
                f"Expected: 'public', 'zone', 'group:<name>', 'user:<id>', or 'agent:<id>'"
            )

    # =========================================================================
    # Helper Methods: Subscriptions
    # =========================================================================

    def _get_subscriptions_path(self, context: Any) -> str:
        """Get path to user's subscriptions config file."""
        return f"/zone/{context.zone_id}/user/{context.user_id}/skill/.subscribed.yaml"

    def _load_subscriptions(self, context: Any) -> list[str]:
        """Load user's subscribed skills from config file.

        Uses TTL-based cache (5s) to avoid repeated YAML parsing.
        Thread-safe: guarded by _subscriptions_lock.
        """
        import yaml

        cache_key = (context.user_id or "", context.zone_id or "")

        # Thread-safe cache check
        with self._subscriptions_lock:
            entry = self._subscriptions_cache.get(cache_key)
            if entry is not None and time.monotonic() < entry.expires_at:
                return list(entry.data)  # Return copy to prevent mutation

        path = self._get_subscriptions_path(context)

        try:
            content = self._fs.sys_read(path, context=context)
            if isinstance(content, bytes):
                content = content.decode("utf-8")
            if content:
                data = yaml.safe_load(content)
                if data:
                    result: list[str] = data.get("subscribed_skills", [])
                    self._cache_subscription(cache_key, result)
                    return list(result)  # Return copy to prevent cache mutation
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            logger.debug(f"Could not load subscribed skills from {path}: {e}")

        self._cache_subscription(cache_key, [])
        return []

    _MAX_SUBSCRIPTION_CACHE_SIZE = 500

    def _cache_subscription(self, key: tuple[str, str], data: list[str]) -> None:
        """Thread-safe subscription cache write with size eviction."""
        with self._subscriptions_lock:
            if len(self._subscriptions_cache) >= self._MAX_SUBSCRIPTION_CACHE_SIZE:
                # Evict expired entries first
                now = time.monotonic()
                expired = [k for k, v in self._subscriptions_cache.items() if now >= v.expires_at]
                for k in expired:
                    del self._subscriptions_cache[k]
                # If still full, clear all
                if len(self._subscriptions_cache) >= self._MAX_SUBSCRIPTION_CACHE_SIZE:
                    self._subscriptions_cache.clear()
            self._subscriptions_cache[key] = _CacheEntry(
                data=data,
                expires_at=time.monotonic() + _DEFAULT_SUBSCRIPTION_TTL,
            )

    def _load_assigned_skills(self, context: Any) -> list[str]:
        """Load agent's assigned skills from config.yaml metadata."""
        import yaml

        agent_id = getattr(context, "agent_id", None) or getattr(context, "subject_id", None)
        if not agent_id or "," not in str(agent_id):
            logger.warning(f"Invalid agent_id format: {agent_id}")
            return []

        user_id, agent_name = agent_id.split(",", 1)
        config_path = f"/zone/{context.zone_id}/user/{user_id}/agent/{agent_name}/config.yaml"

        try:
            content = self._fs.sys_read(config_path, context=context)
            if isinstance(content, bytes):
                content = content.decode("utf-8")
            if content:
                data = yaml.safe_load(content)
                if data and isinstance(data, dict):
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

    def _save_subscriptions(self, context: Any, skills: list[str]) -> None:
        """Save user's subscribed skills to config file."""
        import yaml

        path = self._get_subscriptions_path(context)
        data = {"subscribed_skills": skills}
        content = yaml.dump(data, default_flow_style=False, sort_keys=False)

        parent_dir = "/".join(path.split("/")[:-1])
        try:
            self._fs.sys_mkdir(parent_dir, context=context)
        except Exception as e:
            logger.debug("Failed to create parent directory %s: %s", parent_dir, e)

        self._fs.sys_write(path, content, context=context)

        # Invalidate cache for this user/zone
        cache_key = (context.user_id or "", context.zone_id or "")
        with self._subscriptions_lock:
            self._subscriptions_cache.pop(cache_key, None)

    # =========================================================================
    # Helper Methods: Skill Discovery & Metadata
    # =========================================================================

    def _collect_skill_paths(
        self,
        context: Any,
        *,
        public_paths: list[str] | None = None,
        shared_paths: list[str] | None = None,
    ) -> list[str]:
        """Collect all skill paths from filesystem + ReBAC.

        Uses _find_skills_in_directory for all filesystem scanning
        (Issue #2035 Phase 4.3 — consolidated duplicate scanning).
        """
        skill_paths: list[str] = []

        # System skills
        skill_paths.extend(self._find_skills_in_directory("/skill/", context))

        # Zone skills
        skill_paths.extend(
            self._find_skills_in_directory(f"/zone/{context.zone_id}/skill/", context)
        )

        # User skills
        skill_paths.extend(
            self._find_skills_in_directory(
                f"/zone/{context.zone_id}/user/{context.user_id}/skill/", context
            )
        )

        # Cross-zone public skills
        existing = set(skill_paths)
        for path in public_paths if public_paths is not None else self._find_public_skills():
            if path not in existing:
                skill_paths.append(path)
                existing.add(path)

        # Skills shared directly with the user
        for path in (
            shared_paths if shared_paths is not None else self._find_direct_viewer_skills(context)
        ):
            if path not in existing:
                skill_paths.append(path)
                existing.add(path)

        return skill_paths

    def _find_public_skills(self) -> list[str]:
        """Find all publicly shared skills across all zones."""
        public_paths: list[str] = []
        try:
            tuples = self._perms.rebac_list_tuples(
                subject=("role", "public"),
                relation="direct_viewer",
            )
            for t in tuples:
                obj_type = t.get("object_type")
                obj_id = t.get("object_id")
                if obj_type == "file" and obj_id and "/skill/" in obj_id:
                    skill_path = obj_id if obj_id.endswith("/") else obj_id + "/"
                    public_paths.append(skill_path)
        except Exception as e:
            logger.warning(f"Failed to find public skills: {e}")
        return public_paths

    def _find_skills_in_directory(self, base_dir: str, context: Any) -> list[str]:
        """Find skill directories within a base directory.

        Optimized: builds a set of known SKILL.md paths from the listing
        to avoid per-candidate exists() calls (N+1 → 1 call).
        """
        skill_paths: list[str] = []
        try:
            if not self._fs.sys_access(base_dir, context=context):
                return skill_paths

            items = self._fs.sys_readdir(base_dir, context=context)

            # Build set of SKILL.md paths from listing (avoids N exists() calls)
            item_strs = [str(item) for item in items]
            skill_md_items = {s for s in item_strs if s.endswith("SKILL.md")}

            seen_skills: set[str] = set()
            for item_str in item_strs:
                if not item_str.startswith(base_dir):
                    continue

                relative = item_str[len(base_dir) :]
                skill_name = relative.split("/")[0] if "/" in relative else relative
                if (
                    not skill_name
                    or skill_name.startswith(".")
                    or skill_name.endswith((".md", ".json", ".yaml"))
                ):
                    continue

                if skill_name not in seen_skills:
                    skill_path = f"{base_dir}{skill_name}/"
                    skill_md = f"{skill_path}SKILL.md"
                    if skill_md in skill_md_items:
                        skill_paths.append(skill_path)
                        seen_skills.add(skill_name)
        except Exception as e:
            logger.warning(f"Failed to find skills in {base_dir}: {e}")
        return skill_paths

    def _find_direct_viewer_skills(self, context: Any) -> list[str]:
        """Find skills where user has direct_viewer relation."""
        shared_paths: list[str] = []
        try:
            assert context.user_id is not None
            tuples = self._perms.rebac_list_tuples(
                subject=("user", context.user_id),
                relation="direct_viewer",
            )
            for t in tuples:
                obj_type = t.get("object_type")
                obj_id = t.get("object_id")
                if obj_type == "file" and obj_id and "/skill/" in obj_id:
                    if obj_id.endswith("/SKILL.md") or obj_id.endswith("SKILL.md"):
                        skill_path = obj_id[:-8]
                    else:
                        skill_path = obj_id
                    if not skill_path.endswith("/"):
                        skill_path = skill_path + "/"
                    shared_paths.append(skill_path)
        except Exception as e:
            logger.warning(f"Failed to find shared skills: {e}")
        return shared_paths

    def _load_skill_metadata(
        self, skill_path: str, context: Any, *, is_public: bool = False
    ) -> dict[str, Any]:
        """Load skill metadata from SKILL.md file.

        Uses request-scoped cache with thread-safe access (Issue #2035).
        """
        cache_key = skill_path

        # Thread-safe cache check
        with self._metadata_lock:
            if cache_key in self._metadata_cache:
                return self._metadata_cache[cache_key]

        skill_md_path = f"{skill_path}SKILL.md"

        try:
            content = self._fs.sys_read(skill_md_path, context=context)
            if isinstance(content, bytes):
                content = content.decode("utf-8")
            if not content:
                self._cache_metadata(cache_key, {})
                return {}

            metadata, _ = self._parse_skill_content(content)
            self._cache_metadata(cache_key, metadata)
            return metadata

        except Exception:
            if is_public:
                try:
                    system_ctx = self._make_system_context(context)
                    content = self._fs.sys_read(skill_md_path, context=system_ctx)
                    if isinstance(content, bytes):
                        content = content.decode("utf-8")
                    if content:
                        metadata, _ = self._parse_skill_content(content)
                        self._cache_metadata(cache_key, metadata)
                        return metadata
                except Exception as e:
                    logger.warning(f"Failed to read public skill metadata: {e}")
            self._cache_metadata(cache_key, {})
            return {}

    def _cache_metadata(self, key: str, value: dict[str, Any]) -> None:
        """Thread-safe metadata cache write with max size eviction."""
        with self._metadata_lock:
            if len(self._metadata_cache) >= _MAX_METADATA_CACHE_SIZE:
                self._metadata_cache.clear()
            self._metadata_cache[key] = value

    def _make_system_context(self, context: Any) -> Any:
        """Create system context for reading public skill metadata."""
        if self._create_system_context is not None:
            return self._create_system_context(zone_id=context.zone_id)
        # Fallback to local _SystemContext (no core imports needed)
        return _SystemContext(zone_id=context.zone_id)

    @staticmethod
    def _parse_skill_content(content: str) -> tuple[dict[str, Any], str]:
        """Parse SKILL.md content into metadata and body."""
        import re

        metadata: dict[str, Any] = {}
        body = content

        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                import yaml

                try:
                    metadata = yaml.safe_load(parts[1]) or {}
                except Exception as e:
                    logger.debug("Failed to parse skill YAML frontmatter: %s", e)
                body = parts[2].strip()
        else:
            match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
            if match:
                metadata["name"] = match.group(1).strip()

        return metadata, body

    @staticmethod
    def _format_skills_xml(skills: list[SkillInfo]) -> str:
        """Format skills as XML for system prompt injection."""
        from xml.sax.saxutils import escape, quoteattr

        xml_parts = ["<available_skills>"]
        for skill in skills:
            xml_parts.append(f"  <skill path={quoteattr(skill.path)}>")
            xml_parts.append(f"    <name>{escape(skill.name)}</name>")
            xml_parts.append(f"    <description>{escape(skill.description)}</description>")
            xml_parts.append(f"    <owner>{escape(skill.owner)}</owner>")
            xml_parts.append("  </skill>")
        xml_parts.append("</available_skills>")
        return "\n".join(xml_parts)

    # =========================================================================
    # Async Batch Metadata Loading (Issue #2035, Follow-up 4)
    # True concurrent skill metadata reads using concurrent.futures.
    # =========================================================================

    def _batch_load_skill_metadata(
        self,
        skill_paths: list[str],
        context: Any,
        *,
        public_set: set[str] | None = None,
        max_workers: int = 4,
    ) -> dict[str, dict[str, Any]]:
        """Load metadata for multiple skills concurrently.

        Uses ThreadPoolExecutor to parallelize filesystem reads and YAML
        parsing across multiple skill paths. Results are cached in the
        request-scoped metadata cache.

        Args:
            skill_paths: List of skill paths to load metadata for.
            context: Operation context for filesystem reads.
            public_set: Set of normalized paths that are public (optional).
            max_workers: Max threads for concurrent reads (default 4).

        Returns:
            Dict mapping skill_path → parsed metadata dict.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results: dict[str, dict[str, Any]] = {}

        # Separate cached vs uncached paths (thread-safe cache read)
        uncached: list[str] = []
        with self._metadata_lock:
            for path in skill_paths:
                if path in self._metadata_cache:
                    results[path] = self._metadata_cache[path]
                else:
                    uncached.append(path)

        if not uncached:
            return results

        # Load uncached paths concurrently
        _public = public_set or set()

        def _load_one(path: str) -> tuple[str, dict[str, Any]]:
            is_public = path.rstrip("/") in _public
            metadata = self._load_skill_metadata(path, context, is_public=is_public)
            return path, metadata

        # Use min(max_workers, len(uncached)) to avoid spinning up idle threads
        effective_workers = min(max_workers, len(uncached))

        if effective_workers <= 1:
            # Single path — no need for thread pool
            for path in uncached:
                path, metadata = _load_one(path)
                results[path] = metadata
        else:
            with ThreadPoolExecutor(max_workers=effective_workers) as pool:
                futures = {pool.submit(_load_one, p): p for p in uncached}
                for fut in as_completed(futures):
                    try:
                        path, metadata = fut.result()
                        results[path] = metadata
                    except Exception as e:
                        path = futures[fut]
                        logger.warning("Batch metadata load failed for %s: %s", path, e)
                        results[path] = {}

        return results

    def discover_batch(
        self,
        context: Any | None,
        filter: str = "all",
        *,
        max_metadata_workers: int = 4,
    ) -> list[SkillInfo]:
        """Discover skills with batch metadata loading for large skill sets.

        Same as discover() but uses concurrent metadata loading for better
        performance when there are many skills (>10).
        """
        self._validate_context(context)
        assert context is not None

        subscribed_skills = set(self._load_subscriptions(context))

        if filter in ("subscribed", "owned", "shared", "public"):
            # For single-filter modes, delegate to standard discover
            return self._discover_impl(context, filter)

        # "all" filter: batch-collect paths and pre-compute permission sets
        public_paths = self._find_public_skills()
        shared_paths = self._find_direct_viewer_skills(context)
        public_set = {p.rstrip("/") for p in public_paths}
        shared_set = {p.rstrip("/") for p in shared_paths}

        skill_paths = self._collect_skill_paths(
            context, public_paths=public_paths, shared_paths=shared_paths
        )

        if not skill_paths:
            return []

        # Batch load metadata concurrently
        metadata_map = self._batch_load_skill_metadata(
            skill_paths, context, public_set=public_set, max_workers=max_metadata_workers
        )

        results: list[SkillInfo] = []
        for path in skill_paths:
            normalized = path.rstrip("/")
            is_public = normalized in public_set

            # skip_public_check=True: public_set already pre-computed above
            if (
                not is_public
                and normalized not in shared_set
                and not self._can_read_skill(path, context, skip_public_check=True)
            ):
                continue

            metadata = metadata_map.get(path, {})
            results.append(
                self._build_skill_info(
                    path,
                    metadata,
                    is_subscribed=path in subscribed_skills,
                    is_public=is_public,
                )
            )

        return results

    # =========================================================================
    # RPC-Facing Methods (Issue #2035, Follow-up 1)
    # Auto-discovered by server via @rpc_expose decorator.
    # These wrap the domain methods with dict serialization for JSON-RPC.
    # =========================================================================

    @rpc_expose(name="skills_share", description="Share a skill with users, groups, or make public")
    def rpc_share(
        self,
        skill_path: str,
        share_with: str,
        context: Any | None = None,
    ) -> dict[str, Any]:
        """RPC wrapper for share()."""
        tuple_id = self.share(skill_path, share_with, context)
        return {
            "success": True,
            "tuple_id": tuple_id,
            "skill_path": skill_path,
            "share_with": share_with,
        }

    @rpc_expose(name="skills_unshare", description="Revoke sharing permission on a skill")
    def rpc_unshare(
        self,
        skill_path: str,
        unshare_from: str,
        context: Any | None = None,
    ) -> dict[str, Any]:
        """RPC wrapper for unshare()."""
        success = self.unshare(skill_path, unshare_from, context)
        return {
            "success": success,
            "skill_path": skill_path,
            "unshare_from": unshare_from,
        }

    @rpc_expose(
        name="skills_discover", description="Discover skills the user has permission to see"
    )
    def rpc_discover(
        self,
        filter: str = "all",
        context: Any | None = None,
    ) -> dict[str, Any]:
        """RPC wrapper for discover()."""
        skills = self.discover(context, filter)
        return {
            "skills": [s.to_dict() for s in skills],
            "count": len(skills),
        }

    @rpc_expose(name="skills_subscribe", description="Subscribe to a skill (add to user's library)")
    def rpc_subscribe(
        self,
        skill_path: str,
        context: Any | None = None,
    ) -> dict[str, Any]:
        """RPC wrapper for subscribe()."""
        newly_subscribed = self.subscribe(skill_path, context)
        return {
            "success": True,
            "skill_path": skill_path,
            "already_subscribed": not newly_subscribed,
        }

    @rpc_expose(
        name="skills_unsubscribe",
        description="Unsubscribe from a skill (remove from user's library)",
    )
    def rpc_unsubscribe(
        self,
        skill_path: str,
        context: Any | None = None,
    ) -> dict[str, Any]:
        """RPC wrapper for unsubscribe()."""
        was_subscribed = self.unsubscribe(skill_path, context)
        return {
            "success": True,
            "skill_path": skill_path,
            "was_subscribed": was_subscribed,
        }

    @rpc_expose(
        name="skills_get_prompt_context",
        description="Get skill metadata for system prompt injection",
    )
    def rpc_get_prompt_context(
        self,
        max_skills: int = 50,
        context: Any | None = None,
    ) -> dict[str, Any]:
        """RPC wrapper for get_prompt_context()."""
        prompt_context = self.get_prompt_context(context, max_skills)
        return prompt_context.to_dict()

    @rpc_expose(name="skills_load", description="Load full skill content on-demand")
    def rpc_load(
        self,
        skill_path: str,
        context: Any | None = None,
    ) -> dict[str, Any]:
        """RPC wrapper for load()."""
        content = self.load(skill_path, context)
        return content.to_dict()
