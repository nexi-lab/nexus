"""Nexus Skills System.

The Skills System provides:
- SKILL.md parser with YAML frontmatter support
- Skill registry with progressive disclosure and lazy loading
- Three-tier hierarchy (agent > tenant > system)
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
    >>> # Publish to tenant library
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
from nexus.skills.mcp_exporter import MCPToolExporter
from nexus.skills.mcp_models import (
    MCPMount,
    MCPToolConfig,
    MCPToolDefinition,
    MCPToolExample,
)
from nexus.skills.mcp_mount import MCPMountError, MCPMountManager
from nexus.skills.models import Skill, SkillMetadata
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
