"""Type definitions for the Skills Service.

These dataclasses represent the API response types for skill operations.
They are separate from the internal models (Skill, SkillMetadata) to allow
for independent evolution of internal and external representations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SkillInfo:
    """Skill information returned by discovery operations.

    This is a lightweight representation suitable for listing skills.
    """

    path: str
    name: str
    description: str
    owner: str
    is_subscribed: bool = False
    is_public: bool = False
    version: str | None = None
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "path": self.path,
            "name": self.name,
            "description": self.description,
            "owner": self.owner,
            "is_subscribed": self.is_subscribed,
            "is_public": self.is_public,
            "version": self.version,
            "tags": self.tags,
        }


@dataclass
class SkillContent:
    """Full skill content returned by load operations.

    This includes the complete SKILL.md content for agent use.
    """

    path: str
    name: str
    description: str
    owner: str
    content: str  # Full markdown content (body after frontmatter)
    metadata: dict[str, Any] = field(default_factory=dict)  # Parsed frontmatter
    scripts: list[str] = field(default_factory=list)  # Paths to associated scripts
    references: list[str] = field(default_factory=list)  # Paths to reference files

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "path": self.path,
            "name": self.name,
            "description": self.description,
            "owner": self.owner,
            "content": self.content,
            "metadata": self.metadata,
            "scripts": self.scripts,
            "references": self.references,
        }


@dataclass
class PromptContext:
    """Skill context formatted for system prompt injection.

    This is optimized for low token count (~100 tokens/skill).
    """

    xml: str  # XML-formatted skill list
    skills: list[SkillInfo]
    count: int
    token_estimate: int

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "xml": self.xml,
            "skills": [s.to_dict() for s in self.skills],
            "count": self.count,
            "token_estimate": self.token_estimate,
        }
