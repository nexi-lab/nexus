"""Nexus Skills System.

The Skills System provides:
- SKILL.md parser with YAML frontmatter support
- Skill registry with progressive disclosure and lazy loading
- Three-tier hierarchy (agent > zone > system)
- Dependency resolution with DAG and cycle detection
- Vendor-neutral skill export to .zip packages
- Skill lifecycle management (create, fork, publish)
- Template system for common skill patterns
- MCP tool integration for dynamic tool discovery

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
    >>> # Publish to zone library
    >>> await manager.publish_skill("my-skill")
    >>>
    >>> # Export skill
    >>> exporter = SkillExporter(registry)
    >>> await exporter.export_skill("analyze-code", "output.zip", format="claude")
    >>>
    >>> # MCP Tools Integration
    >>> from nexus.skills import MCPToolExporter, MCPMountManager
    >>>
    >>> # Export Nexus MCP tools as skills
    >>> mcp_exporter = MCPToolExporter(nx)
    >>> await mcp_exporter.export_nexus_tools()
    >>>
    >>> # Mount external MCP servers
    >>> mcp_manager = MCPMountManager(nx)
    >>> await mcp_manager.mount(MCPMount(
    ...     name="github",
    ...     transport="stdio",
    ...     command="npx",
    ...     args=["-y", "@modelcontextprotocol/server-github"]
    ... ))
    >>> await mcp_manager.sync_tools("github")
"""

import importlib

# Eager imports — commonly used classes that should load immediately
from nexus.skills.manager import SkillManager, SkillManagerError
from nexus.skills.models import Skill, SkillMetadata
from nexus.skills.parser import SkillParseError, SkillParser
from nexus.skills.registry import (
    SkillDependencyError,
    SkillNotFoundError,
    SkillRegistry,
)

# Lazy imports — loaded on first access via __getattr__
_LAZY_IMPORTS: dict[str, str] = {
    # Analytics
    "SkillAnalyticsTracker": "nexus.skills.analytics",
    "SkillAnalytics": "nexus.skills.analytics",
    "SkillUsageRecord": "nexus.skills.analytics",
    "DashboardMetrics": "nexus.skills.analytics",
    # Governance
    "SkillGovernance": "nexus.skills.governance",
    "SkillApproval": "nexus.skills.governance",
    "ApprovalStatus": "nexus.skills.governance",
    "GovernanceError": "nexus.skills.governance",
    # Audit
    "SkillAuditLogger": "nexus.skills.audit",
    "AuditLogEntry": "nexus.skills.audit",
    "AuditAction": "nexus.skills.audit",
    # Exporter
    "SkillExporter": "nexus.skills.exporter",
    "SkillExportError": "nexus.skills.exporter",
    # Protocols
    "NexusFilesystem": "nexus.skills.protocols",
    # Templates
    "get_template": "nexus.skills.templates",
    "list_templates": "nexus.skills.templates",
    "get_template_description": "nexus.skills.templates",
    "TemplateError": "nexus.skills.templates",
    # MCP Integration (backward-compat re-exports, moved to nexus.mcp)
    "MCPToolConfig": "nexus.mcp.models",
    "MCPToolDefinition": "nexus.mcp.models",
    "MCPToolExample": "nexus.mcp.models",
    "MCPMount": "nexus.mcp.models",
    "MCPMountManager": "nexus.mcp.mount",
    "MCPMountError": "nexus.mcp.mount",
    "MCPToolExporter": "nexus.mcp.exporter",
}


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
    # MCP Integration
    "MCPToolConfig",
    "MCPToolDefinition",
    "MCPToolExample",
    "MCPMount",
    "MCPMountManager",
    "MCPMountError",
    "MCPToolExporter",
]
