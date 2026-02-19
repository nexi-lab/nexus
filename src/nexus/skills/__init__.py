"""Nexus Skills System — backward compatibility shim.

Issue #2035, Follow-up 3: Skills brick moved to nexus.bricks.skills.
This module re-exports everything from the canonical location for
backward compatibility. New code should import from nexus.bricks.skills.
"""

from __future__ import annotations

import importlib
import importlib.machinery
import sys
from typing import TYPE_CHECKING

# -------------------------------------------------------------------------
# Submodule mapping for `from nexus.skills.X import Y` compatibility
# MUST be defined and installed BEFORE any eager imports, because the
# import chain (e.g. bricks.skills.protocols → services → skill_service
# → nexus.skills.service) may need the finder during our own __init__.
# -------------------------------------------------------------------------

_SUBMODULE_MAP: dict[str, str] = {
    "nexus.skills.analytics": "nexus.bricks.skills.analytics",
    "nexus.skills.audit": "nexus.bricks.skills.audit",
    "nexus.skills.exceptions": "nexus.bricks.skills.exceptions",
    "nexus.skills.exporter": "nexus.bricks.skills.exporter",
    "nexus.skills.governance": "nexus.bricks.skills.governance",
    "nexus.skills.importer": "nexus.bricks.skills.importer",
    "nexus.skills.manager": "nexus.bricks.skills.manager",
    "nexus.skills.models": "nexus.bricks.skills.models",
    "nexus.skills.package_service": "nexus.bricks.skills.package_service",
    "nexus.skills.parser": "nexus.bricks.skills.parser",
    "nexus.skills.protocols": "nexus.bricks.skills.protocols",
    "nexus.skills.registry": "nexus.bricks.skills.registry",
    "nexus.skills.service": "nexus.bricks.skills.service",
    "nexus.skills.skill_generator": "nexus.bricks.skills.skill_generator",
    "nexus.skills.templates": "nexus.bricks.skills.templates",
    "nexus.skills.testing": "nexus.bricks.skills.testing",
    "nexus.skills.types": "nexus.bricks.skills.types",
}


class _SkillsSubmoduleLoader:
    """PEP 451 loader that delegates to the canonical bricks.skills module."""

    def __init__(self, canonical: str) -> None:
        self._canonical = canonical

    def create_module(self, spec: object) -> object:
        return importlib.import_module(self._canonical)

    def exec_module(self, module: object) -> None:
        pass  # Module already loaded by create_module


class _SkillsSubmoduleFinder:
    """PEP 451 import finder that redirects nexus.skills.X -> nexus.bricks.skills.X."""

    def find_spec(self, fullname: str, path: object = None, target: object = None) -> object:
        if fullname in _SUBMODULE_MAP:
            canonical = _SUBMODULE_MAP[fullname]
            return importlib.machinery.ModuleSpec(
                fullname,
                _SkillsSubmoduleLoader(canonical),  # type: ignore[arg-type]
            )
        return None


# Install the finder BEFORE eager imports (import chain may need it)
if not any(isinstance(f, _SkillsSubmoduleFinder) for f in sys.meta_path):
    sys.meta_path.append(_SkillsSubmoduleFinder())  # type: ignore[arg-type]


# -------------------------------------------------------------------------
# Eager imports — re-export from canonical brick location
# -------------------------------------------------------------------------
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
    "SkillRegistryProtocol": "nexus.bricks.skills.protocols",
    "SkillManagerProtocol": "nexus.bricks.skills.protocols",
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
    from nexus.bricks.skills.protocols import (
        NexusFilesystem,
        SkillManagerProtocol,
        SkillRegistryProtocol,
    )
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
    raise AttributeError(f"module 'nexus.skills' has no attribute {name}")


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
    "SkillRegistryProtocol",
    "SkillManagerProtocol",
    # Service (Issue #2035)
    "SkillService",
    "SkillPackageService",
]
