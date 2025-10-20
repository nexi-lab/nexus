"""Nexus Skills System.

The Skills System provides:
- SKILL.md parser with YAML frontmatter support
- Skill registry with progressive disclosure and lazy loading
- Three-tier hierarchy (agent > tenant > system)
- Dependency resolution with DAG and cycle detection
- Vendor-neutral skill export to .zip packages
- Skill lifecycle management (create, fork, publish)
- Template system for common skill patterns

Example:
    >>> from nexus import connect
    >>> from nexus.skills import SkillRegistry, SkillManager, SkillExporter
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
    >>> # Create new skill from template
    >>> manager = SkillManager(nx, registry)
    >>> await manager.create_skill(
    ...     "my-skill",
    ...     description="My custom skill",
    ...     template="basic"
    ... )
    >>>
    >>> # Fork existing skill
    >>> await manager.fork_skill("analyze-code", "my-analyzer")
    >>>
    >>> # Publish to tenant library
    >>> await manager.publish_skill("my-skill")
    >>>
    >>> # Export skill
    >>> exporter = SkillExporter(registry)
    >>> await exporter.export_skill("analyze-code", "output.zip", format="claude")
"""

from nexus.skills.analytics import (
    DashboardMetrics,
    SkillAnalytics,
    SkillAnalyticsTracker,
    SkillUsageRecord,
)
from nexus.skills.audit import AuditAction, AuditLogEntry, SkillAuditLogger
from nexus.skills.exporter import SkillExporter, SkillExportError
from nexus.skills.governance import (
    ApprovalStatus,
    GovernanceError,
    SkillApproval,
    SkillGovernance,
)
from nexus.skills.manager import SkillManager, SkillManagerError
from nexus.skills.models import Skill, SkillExportManifest, SkillMetadata
from nexus.skills.parser import SkillParseError, SkillParser
from nexus.skills.protocols import NexusFilesystem
from nexus.skills.registry import (
    SkillDependencyError,
    SkillNotFoundError,
    SkillRegistry,
)
from nexus.skills.templates import (
    TemplateError,
    get_template,
    get_template_description,
    list_templates,
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
    # Manager
    "SkillManager",
    "SkillManagerError",
    # Templates
    "get_template",
    "list_templates",
    "get_template_description",
    "TemplateError",
    # Analytics
    "SkillAnalyticsTracker",
    "SkillAnalytics",
    "SkillUsageRecord",
    "DashboardMetrics",
    # Governance
    "SkillGovernance",
    "SkillApproval",
    "ApprovalStatus",
    "GovernanceError",
    # Audit
    "SkillAuditLogger",
    "AuditLogEntry",
    "AuditAction",
    # Protocols
    "NexusFilesystem",
]
