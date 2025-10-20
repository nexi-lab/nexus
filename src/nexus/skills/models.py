"""Data models for the Skills System."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


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
        if self.tier and self.tier not in ("agent", "tenant", "system"):
            raise ValidationError(
                f"skill tier must be 'agent', 'tenant', or 'system', got '{self.tier}'"
            )


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


@dataclass
class SkillExportManifest:
    """Manifest for exported skill packages.

    Used when exporting skills to .zip format.
    """

    # Skill identification
    name: str
    version: str | None = None
    description: str = ""

    # Export metadata
    format: str = "generic"  # generic, claude, openai, etc.
    exported_at: datetime | None = None

    # Files included in export
    files: list[str] = field(default_factory=list)

    # Total size
    total_size_bytes: int = 0

    def validate(self) -> None:
        """Validate export manifest.

        Raises:
            ValidationError: If validation fails with clear message.
        """
        from nexus.core.exceptions import ValidationError

        if not self.name:
            raise ValidationError("export manifest name is required")

        if not self.files:
            raise ValidationError(
                f"export manifest must include at least one file for '{self.name}'"
            )

        # Validate Claude format size limit (8MB)
        if self.format == "claude" and self.total_size_bytes > 8 * 1024 * 1024:
            raise ValidationError(
                f"skill '{self.name}' exceeds Claude format 8MB limit: "
                f"{self.total_size_bytes / (1024 * 1024):.2f}MB"
            )
