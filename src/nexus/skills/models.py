"""Data models for the Skills System."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.skills.mcp_models import MCPToolConfig


@dataclass
class SkillMetadata:
    """Metadata for a skill (lightweight, loaded during discovery).

    This represents the YAML frontmatter in SKILL.md files.
    Progressive disclosure: Load metadata first, full content on-demand.
    """

    # Required fields
    name: str
    description: str

    # Optional Nexus-specific fields
    version: str | None = None
    author: str | None = None
    created_at: datetime | None = None
    modified_at: datetime | None = None

    # Skill dependencies
    requires: list[str] = field(default_factory=list)

    # Skill type: "documentation" | "mcp_tool" | "hybrid"
    # - documentation: Traditional skill with markdown content
    # - mcp_tool: MCP tool exposed as a skill
    # - hybrid: Both documentation and MCP tool
    skill_type: str = "documentation"

    # MCP tool configuration (for skill_type="mcp_tool" or "hybrid")
    mcp_config: MCPToolConfig | None = None

    # Tags for categorization
    tags: list[str] = field(default_factory=list)

    # Additional metadata (extensible)
    metadata: dict[str, Any] = field(default_factory=dict)

    # Internal fields
    file_path: str | None = None  # Path to SKILL.md file
    tier: str | None = None  # agent, tenant, or system

    def validate(self) -> None:
        """Validate skill metadata.

        Raises:
            ValidationError: If validation fails with clear message.
        """
        from nexus.core.exceptions import ValidationError

        # Validate required fields
        if not self.name:
            raise ValidationError("skill name is required")

        if not self.description:
            raise ValidationError(f"skill description is required for '{self.name}'")

        # Validate name format (alphanumeric, dash, underscore only)
        if not self.name.replace("-", "").replace("_", "").isalnum():
            raise ValidationError(
                f"skill name must be alphanumeric (with - or _), got '{self.name}'"
            )

        # Validate tier if provided
        # Accept personal/user as valid tiers (personal is alias for user)
        valid_tiers = ("agent", "tenant", "system", "personal", "user")
        if self.tier and self.tier not in valid_tiers:
            raise ValidationError(f"skill tier must be one of {valid_tiers}, got '{self.tier}'")

        # Validate skill_type
        valid_skill_types = ("documentation", "mcp_tool", "hybrid")
        if self.skill_type not in valid_skill_types:
            raise ValidationError(
                f"skill_type must be one of {valid_skill_types}, got '{self.skill_type}'"
            )

        # Validate mcp_config is provided for mcp_tool or hybrid types
        if self.skill_type in ("mcp_tool", "hybrid") and self.mcp_config is None:
            raise ValidationError(f"mcp_config is required for skill_type '{self.skill_type}'")


@dataclass
class Skill:
    """Complete skill representation (metadata + content).

    Lazy loading: Created only when full skill content is requested.
    """

    # Metadata (lightweight)
    metadata: SkillMetadata

    # Full content (heavy)
    content: str  # Markdown content (everything after frontmatter)

    def validate(self) -> None:
        """Validate complete skill.

        Raises:
            ValidationError: If validation fails with clear message.
        """
        from nexus.core.exceptions import ValidationError

        # Validate metadata
        self.metadata.validate()

        # Validate content
        if not self.content:
            raise ValidationError(f"skill content is required for '{self.metadata.name}'")
