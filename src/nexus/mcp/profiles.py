"""Tool profile configuration and ReBAC grant generation (Issue #1272).

Defines named tool presets (e.g., "minimal", "coding", "full") that map to
sets of MCP tools. Profiles support single-level inheritance via ``extends``.

Architecture:
    Config YAML (policy) → ToolProfileConfig (in-memory) → ReBAC grants (enforcement)

Design decisions:
    - Profiles are config (YAML, version-controlled). ReBAC is enforcement.
    - Inheritance is resolved eagerly at load time (not at query time).
    - Cycle detection prevents infinite loops in ``extends`` chains.
    - Grant generation is a one-time batch write at agent creation.
    - Unknown tool names in profiles emit a warning (not an error) to allow
      profiles to reference tools from optional MCP servers.

References:
    - Issue #1272: MCP tool-level namespace — per-tool ReBAC grants
    - Stripe Minions "Toolshed" pattern
    - Progent DSL: declarative per-tool policies
    - Cerbos: config is policy, PDP is enforcement
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.services.permissions.rebac_manager_enhanced import (
        EnhancedReBACManager,
        WriteResult,
    )

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

TOOL_PATH_PREFIX = "/tools/"
"""Namespace prefix for MCP tools. Tools live at /tools/{tool_name}."""


@dataclass(frozen=True, slots=True)
class ToolProfile:
    """A named collection of MCP tools.

    Attributes:
        name: Profile identifier (e.g., "minimal", "coding").
        tools: Resolved (flattened) frozenset of tool names.
        extends: Parent profile name (None if root profile).
        description: Human-readable description.
    """

    name: str
    tools: frozenset[str]
    extends: str | None = None
    description: str = ""

    def tool_paths(self) -> frozenset[str]:
        """Return tool names as namespace paths (e.g., '/tools/nexus_read_file')."""
        return frozenset(f"{TOOL_PATH_PREFIX}{t}" for t in self.tools)


@dataclass(frozen=True, slots=True)
class ToolProfileConfig:
    """Resolved collection of tool profiles.

    Attributes:
        profiles: Mapping of profile name → resolved ToolProfile.
        default_profile: Name of the default profile for new agents.
    """

    profiles: dict[str, ToolProfile]
    default_profile: str = "minimal"

    def get_profile(self, name: str) -> ToolProfile | None:
        """Look up a profile by name."""
        return self.profiles.get(name)

    def get_default(self) -> ToolProfile | None:
        """Return the default profile."""
        return self.profiles.get(self.default_profile)

    @property
    def profile_names(self) -> list[str]:
        """Sorted list of profile names."""
        return sorted(self.profiles)


# ---------------------------------------------------------------------------
# Inheritance resolution
# ---------------------------------------------------------------------------


class ProfileCycleError(ValueError):
    """Raised when profile inheritance contains a cycle."""


class ProfileNotFoundError(KeyError):
    """Raised when a profile references a non-existent parent."""


def resolve_inheritance(
    raw_profiles: dict[str, dict[str, Any]],
) -> dict[str, ToolProfile]:
    """Resolve profile inheritance and return flat ToolProfile map.

    Algorithm:
        1. Topological sort via DFS to detect cycles.
        2. Resolve bottom-up: root profiles first, then children.
        3. Child tools = parent tools | child-specific tools.

    Args:
        raw_profiles: Mapping of profile name → raw dict with keys:
            - tools: list[str] (tool names specific to this profile)
            - extends: str | None (parent profile name)
            - description: str (optional)

    Returns:
        Mapping of profile name → resolved ToolProfile with flattened tools.

    Raises:
        ProfileCycleError: If inheritance chain contains a cycle.
        ProfileNotFoundError: If a profile extends a non-existent parent.
    """
    resolved: dict[str, ToolProfile] = {}
    visiting: set[str] = set()

    def _resolve(name: str) -> ToolProfile:
        if name in resolved:
            return resolved[name]

        if name not in raw_profiles:
            raise ProfileNotFoundError(
                f"Profile '{name}' not found. Available profiles: {sorted(raw_profiles.keys())}"
            )

        if name in visiting:
            raise ProfileCycleError(
                f"Cycle detected in profile inheritance: "
                f"'{name}' is part of a cycle involving {sorted(visiting)}"
            )

        visiting.add(name)
        raw = raw_profiles[name]
        extends = raw.get("extends")
        own_tools = frozenset(raw.get("tools", []))
        description = raw.get("description", "")

        if extends is not None:
            parent = _resolve(extends)
            all_tools = parent.tools | own_tools
        else:
            all_tools = own_tools

        visiting.discard(name)

        profile = ToolProfile(
            name=name,
            tools=all_tools,
            extends=extends,
            description=description,
        )
        resolved[name] = profile
        return profile

    for profile_name in raw_profiles:
        _resolve(profile_name)

    return resolved


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------


def load_profiles(config_path: Path) -> ToolProfileConfig:
    """Load tool profiles from a YAML config file.

    Args:
        config_path: Path to YAML file with profile definitions.

    Returns:
        Resolved ToolProfileConfig.

    Raises:
        FileNotFoundError: If config_path does not exist.
        ProfileCycleError: If inheritance contains a cycle.
        ProfileNotFoundError: If a profile extends a non-existent parent.
        yaml.YAMLError: If YAML is malformed.
    """
    import yaml

    if not config_path.exists():
        raise FileNotFoundError(f"Tool profile config not found: {config_path}")

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"Expected YAML dict, got {type(raw).__name__}")

    raw_profiles = raw.get("profiles", {})
    default_profile = raw.get("default_profile", "minimal")

    if not raw_profiles:
        logger.warning("[PROFILES] No profiles defined in %s", config_path)
        return ToolProfileConfig(profiles={}, default_profile=default_profile)

    profiles = resolve_inheritance(raw_profiles)

    logger.info(
        "[PROFILES] Loaded %d profiles from %s (default: %s)",
        len(profiles),
        config_path,
        default_profile,
    )

    return ToolProfileConfig(profiles=profiles, default_profile=default_profile)


def load_profiles_from_dict(raw: dict[str, Any]) -> ToolProfileConfig:
    """Load tool profiles from an already-parsed dict (for testing/embedding).

    Args:
        raw: Dict with keys "profiles" and optional "default_profile".

    Returns:
        Resolved ToolProfileConfig.
    """
    raw_profiles = raw.get("profiles", {})
    default_profile = raw.get("default_profile", "minimal")

    if not raw_profiles:
        return ToolProfileConfig(profiles={}, default_profile=default_profile)

    profiles = resolve_inheritance(raw_profiles)
    return ToolProfileConfig(profiles=profiles, default_profile=default_profile)


# ---------------------------------------------------------------------------
# ReBAC grant generation
# ---------------------------------------------------------------------------


def grant_tools_for_profile(
    rebac_manager: EnhancedReBACManager,
    subject: tuple[str, str],
    profile: ToolProfile,
    zone_id: str | None = None,
) -> list[WriteResult]:
    """Batch-write ReBAC grants for all tools in a profile.

    Creates tuples: (subject, "direct_viewer", ("file", "/tools/{tool_name}"))

    Args:
        rebac_manager: ReBAC manager for writing tuples.
        subject: (subject_type, subject_id) tuple (e.g., ("agent", "A")).
        profile: Resolved ToolProfile with flattened tool set.
        zone_id: Optional zone ID for multi-zone isolation.

    Returns:
        List of WriteResult objects (one per tool grant).
    """
    results: list[WriteResult] = []

    for tool_name in sorted(profile.tools):
        tool_path = f"{TOOL_PATH_PREFIX}{tool_name}"
        result = rebac_manager.rebac_write(
            subject=subject,
            relation="direct_viewer",
            object=("file", tool_path),
            zone_id=zone_id,
        )
        results.append(result)

    logger.info(
        "[PROFILES] Granted %d tools to %s:%s (profile: %s, zone: %s)",
        len(results),
        subject[0],
        subject[1],
        profile.name,
        zone_id,
    )

    return results


def revoke_tools_by_tuple_ids(
    rebac_manager: EnhancedReBACManager,
    tuple_ids: list[str],
) -> int:
    """Revoke tool grants by their tuple IDs.

    Use the tuple_ids returned by ``grant_tools_for_profile()`` (via
    ``WriteResult.tuple_id``) to cleanly revoke grants.

    Args:
        rebac_manager: ReBAC manager for deleting tuples.
        tuple_ids: List of tuple IDs to delete.

    Returns:
        Number of tuples actually deleted.
    """
    deleted = 0

    for tid in tuple_ids:
        if rebac_manager.rebac_delete(tid):
            deleted += 1

    logger.info(
        "[PROFILES] Revoked %d of %d tool grants",
        deleted,
        len(tuple_ids),
    )

    return deleted
