"""Nexus Skills System.

The Skills System provides:
- SKILL.md parser with YAML frontmatter support
- Skill registry with progressive disclosure and lazy loading
- Three-tier hierarchy (agent > zone > system)
- Dependency resolution with DAG and cycle detection
- Vendor-neutral skill export to .zip packages
- Skill lifecycle management (create, fork, publish)
- Template system for common skill patterns

Issue #2035: Skills extracted into a proper brick with protocol boundaries.
- SkillService: Distribution + Subscription + Runner APIs
- SkillPackageService: Export + Import + Validation
- NexusFilesystem protocol moved to services/protocols/filesystem.py
"""

import importlib
from typing import TYPE_CHECKING

# Eager imports — commonly used classes that should load immediately
from nexus.bricks.skills.manager import SkillManager, SkillManagerError
from nexus.bricks.skills.models import Skill, SkillMetadata
from nexus.bricks.skills.parser import SkillParseError, SkillParser
from nexus.bricks.skills.registry import (
    SkillDependencyError,
    SkillNotFoundError,
    SkillRegistry,
)

# Lazy imports — loaded on first access via __getattr__
_LAZY_IMPORTS: dict[str, str] = {
    # Analytics
    "SkillAnalyticsTracker": "nexus.bricks.skills.analytics",
    "SkillAnalytics": "nexus.bricks.skills.analytics",
    "SkillUsageRecord": "nexus.bricks.skills.analytics",
    "DashboardMetrics": "nexus.bricks.skills.analytics",
    # Governance
    "SkillGovernance": "nexus.bricks.skills.governance",
    "SkillApproval": "nexus.bricks.skills.governance",
    "ApprovalStatus": "nexus.bricks.skills.governance",
    "GovernanceError": "nexus.bricks.skills.governance",
    # Audit
    "SkillAuditLogger": "nexus.bricks.skills.audit",
    "AuditLogEntry": "nexus.bricks.skills.audit",
    "AuditAction": "nexus.bricks.skills.audit",
    # Exporter
    "SkillExporter": "nexus.bricks.skills.exporter",
    "SkillExportError": "nexus.bricks.skills.exporter",
    # Protocols (backward compat — canonical location: services/protocols/filesystem.py)
    "NexusFilesystem": "nexus.bricks.skills.protocols",
    # Templates
    "get_template": "nexus.bricks.skills.templates",
    "list_templates": "nexus.bricks.skills.templates",
    "get_template_description": "nexus.bricks.skills.templates",
    "TemplateError": "nexus.bricks.skills.templates",
    # Service (Issue #2035)
    "SkillService": "nexus.bricks.skills.service",
    "SkillPackageService": "nexus.bricks.skills.package_service",
}


# TYPE_CHECKING imports — lets mypy resolve lazy types without runtime cost
if TYPE_CHECKING:
    from nexus.bricks.skills.analytics import (
        DashboardMetrics,
        SkillAnalytics,
        SkillAnalyticsTracker,
        SkillUsageRecord,
    )
    from nexus.bricks.skills.audit import AuditAction, AuditLogEntry, SkillAuditLogger
    from nexus.bricks.skills.exporter import SkillExporter, SkillExportError
    from nexus.bricks.skills.governance import (
        ApprovalStatus,
        GovernanceError,
        SkillApproval,
        SkillGovernance,
    )
    from nexus.bricks.skills.package_service import SkillPackageService
    from nexus.bricks.skills.protocols import NexusFilesystem
    from nexus.bricks.skills.service import SkillService
    from nexus.bricks.skills.templates import (
        TemplateError,
        get_template,
        get_template_description,
        list_templates,
    )


def __getattr__(name: str) -> object:
    if name in _LAZY_IMPORTS:
        module = importlib.import_module(_LAZY_IMPORTS[name])
        return getattr(module, name)
    raise AttributeError(f"module 'nexus.bricks.skills' has no attribute {name}")


__all__ = [
    # Models
    "Skill",
    "SkillMetadata",
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
    # Service (Issue #2035)
    "SkillService",
    "SkillPackageService",
]
