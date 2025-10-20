"""Nexus Skills System.

The Skills System provides:
- SKILL.md parser with YAML frontmatter support
- Skill registry with progressive disclosure and lazy loading
- Three-tier hierarchy (agent > tenant > system)
- Dependency resolution with DAG and cycle detection
- Vendor-neutral skill export to .zip packages

Example:
    >>> from nexus import connect
    >>> from nexus.skills import SkillRegistry, SkillExporter
    >>>
    >>> # Create registry
    >>> nx = connect()
    >>> registry = SkillRegistry(nx)
    >>>
    >>> # Discover skills (loads metadata only)
    >>> await registry.discover()
    >>>
    >>> # Get skill (loads full content)
    >>> skill = await registry.get_skill("analyze-code")
    >>> print(skill.metadata.description)
    >>> print(skill.content)
    >>>
    >>> # Resolve dependencies
    >>> deps = await registry.resolve_dependencies("analyze-code")
    >>>
    >>> # Export skill
    >>> exporter = SkillExporter(registry)
    >>> await exporter.export_skill("analyze-code", "output.zip", format="claude")
"""

from nexus.skills.exporter import SkillExporter, SkillExportError
from nexus.skills.models import Skill, SkillExportManifest, SkillMetadata
from nexus.skills.parser import SkillParseError, SkillParser
from nexus.skills.registry import (
    SkillDependencyError,
    SkillNotFoundError,
    SkillRegistry,
)

__all__ = [
    # Models
    "Skill",
    "SkillMetadata",
    "SkillExportManifest",
    # Parser
    "SkillParser",
    "SkillParseError",
    # Registry
    "SkillRegistry",
    "SkillNotFoundError",
    "SkillDependencyError",
    # Exporter
    "SkillExporter",
    "SkillExportError",
]
