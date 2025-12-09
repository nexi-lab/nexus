"""Skill Generator for unified SKILL.md generation.

This module handles:
- Loading connector skill templates from configs/connector-skills/
- Generating unified SKILL.md combining connector info + MCP tools
- Replacing {mount_path} placeholders with actual mount paths

Example:
    >>> from nexus.skills.skill_generator import SkillGenerator
    >>>
    >>> generator = SkillGenerator()
    >>>
    >>> # Generate connector-only skill
    >>> skill_md = generator.generate_skill_md(
    ...     service_name="google-drive",
    ...     mount_path="/mnt/gdrive/",
    ... )
    >>>
    >>> # Generate skill with MCP tools
    >>> skill_md = generator.generate_skill_md(
    ...     service_name="google-drive",
    ...     mount_path="/mnt/gdrive/",
    ...     mcp_tools=[{"name": "search", "description": "Search files"}],
    ... )
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from nexus.backends.service_map import SERVICE_REGISTRY, ServiceMap

if TYPE_CHECKING:
    from nexus.skills.mcp_models import MCPMount, MCPToolDefinition

logger = logging.getLogger(__name__)

# Default path for connector skill templates
CONNECTOR_SKILLS_PATH = Path(__file__).parent.parent.parent.parent / "configs" / "connector-skills"


@dataclass
class ConnectorTemplate:
    """Parsed connector skill template."""

    name: str
    service: str
    connector: str
    oauth_provider: str | None
    capabilities: list[str]
    content: str  # Full markdown content (after frontmatter)
    raw_content: str  # Original file content


@dataclass
class SkillMetadata:
    """Metadata for a generated skill."""

    name: str
    display_name: str
    description: str
    version: str = "1.0.0"
    skill_type: str = "service"
    capabilities: list[str] = field(default_factory=list)
    connector: str | None = None
    mcp_name: str | None = None
    oauth_provider: str | None = None
    transport: str | None = None
    tool_count: int = 0


class SkillGenerator:
    """Generator for unified SKILL.md content.

    Combines connector templates with MCP tools to create comprehensive
    skill documentation for AI agents.
    """

    def __init__(self, templates_path: Path | None = None):
        """Initialize the skill generator.

        Args:
            templates_path: Optional path to connector skill templates.
                          Defaults to configs/connector-skills/
        """
        self._templates_path = templates_path or CONNECTOR_SKILLS_PATH
        self._templates: dict[str, ConnectorTemplate] = {}
        self._load_templates()

    def _load_templates(self) -> None:
        """Load all connector skill templates."""
        if not self._templates_path.exists():
            logger.warning(f"Templates path does not exist: {self._templates_path}")
            return

        for template_file in self._templates_path.glob("*.md"):
            try:
                template = self._parse_template(template_file)
                if template and template.name:
                    # Index by service name (from frontmatter)
                    self._templates[template.name] = template
                    # Also index by connector name if available from ServiceMap
                    service_info = SERVICE_REGISTRY.get(template.name)
                    if service_info and service_info.connector:
                        self._templates[service_info.connector] = template
                    logger.debug(f"Loaded template: {template_file.name} -> {template.name}")
            except Exception as e:
                logger.warning(f"Failed to parse template {template_file}: {e}")

    def _parse_template(self, template_file: Path) -> ConnectorTemplate | None:
        """Parse a connector skill template file.

        Args:
            template_file: Path to template markdown file

        Returns:
            Parsed ConnectorTemplate or None if parsing fails
        """
        content = template_file.read_text()

        # Parse YAML frontmatter
        if not content.startswith("---"):
            logger.warning(f"Template missing frontmatter: {template_file}")
            return None

        # Find end of frontmatter
        parts = content.split("---", 2)
        if len(parts) < 3:
            logger.warning(f"Invalid frontmatter in: {template_file}")
            return None

        frontmatter_yaml = parts[1].strip()
        body = parts[2].strip()

        try:
            frontmatter = yaml.safe_load(frontmatter_yaml)
        except yaml.YAMLError as e:
            logger.warning(f"Invalid YAML in {template_file}: {e}")
            return None

        # Get service name from frontmatter
        name = frontmatter.get("name", "")

        # Look up service info from ServiceMap to get connector and other info
        service_info = SERVICE_REGISTRY.get(name)

        return ConnectorTemplate(
            name=name,
            service=name,  # service name is same as name in new format
            connector=service_info.connector or "" if service_info else "",
            oauth_provider=service_info.oauth_provider
            if service_info
            else frontmatter.get("oauth_provider"),
            capabilities=service_info.capabilities
            if service_info
            else frontmatter.get("capabilities", []),
            content=body,
            raw_content=content,
        )

    def get_template(self, key: str) -> ConnectorTemplate | None:
        """Get a connector template by connector name or service name.

        Args:
            key: Connector name (e.g., "gdrive_connector") or service name (e.g., "google-drive")

        Returns:
            ConnectorTemplate or None if not found
        """
        return self._templates.get(key)

    def generate_skill_md(
        self,
        service_name: str,
        mount_path: str,
        mcp_tools: list[dict[str, Any]] | None = None,
        mcp_mount: MCPMount | None = None,
        tool_defs: list[MCPToolDefinition] | None = None,
    ) -> str:
        """Generate unified SKILL.md content.

        Combines connector template (if available) with MCP tools.

        Args:
            service_name: Unified service name (e.g., "google-drive")
            mount_path: Mount path for this service (e.g., "/mnt/gdrive/")
            mcp_tools: Optional list of MCP tool dicts with name, description, inputSchema
            mcp_mount: Optional MCPMount for MCP-specific metadata
            tool_defs: Optional list of MCPToolDefinition objects

        Returns:
            Generated SKILL.md content
        """
        # Get service info
        service_info = ServiceMap.get_service_info(service_name)

        # Try to get connector template
        template = None
        if service_info and service_info.connector:
            template = self.get_template(service_info.connector)
        if not template:
            template = self.get_template(service_name)

        # Build metadata
        metadata = self._build_metadata(service_name, service_info, mcp_mount, tool_defs)

        # Generate content
        if template:
            # Use connector template as base
            return self._generate_with_template(
                template, mount_path, metadata, mcp_tools, tool_defs
            )
        elif mcp_tools or tool_defs:
            # MCP-only (no connector template)
            return self._generate_mcp_only(mount_path, metadata, mcp_tools, tool_defs, mcp_mount)
        else:
            # Fallback to basic skill
            return self._generate_basic(mount_path, metadata)

    def _build_metadata(
        self,
        service_name: str,
        service_info: Any | None,
        mcp_mount: MCPMount | None,
        tool_defs: list[MCPToolDefinition] | None,
    ) -> SkillMetadata:
        """Build skill metadata from available sources."""
        if service_info:
            capabilities = service_info.capabilities.copy()
            display_name = service_info.display_name
            description = service_info.description
            connector = service_info.connector
            mcp_name = service_info.klavis_mcp
            oauth_provider = service_info.oauth_provider
        else:
            capabilities = []
            display_name = service_name.replace("-", " ").title()
            description = f"{display_name} integration"
            connector = None
            mcp_name = None
            oauth_provider = None

        if mcp_mount:
            description = mcp_mount.description or description
            if "tools" not in capabilities:
                capabilities.append("tools")

        return SkillMetadata(
            name=service_name,
            display_name=display_name,
            description=description,
            capabilities=capabilities,
            connector=connector,
            mcp_name=mcp_name,
            oauth_provider=oauth_provider,
            transport=mcp_mount.transport if mcp_mount else None,
            tool_count=len(tool_defs) if tool_defs else 0,
        )

    def _generate_with_template(
        self,
        template: ConnectorTemplate,
        mount_path: str,
        metadata: SkillMetadata,
        mcp_tools: list[dict[str, Any]] | None,
        tool_defs: list[MCPToolDefinition] | None,
    ) -> str:
        """Generate SKILL.md using connector template as base."""
        # Replace {mount_path} placeholder in both content and description
        content = template.content.replace("{mount_path}", mount_path)

        # Get description from template raw content (frontmatter)
        # Parse description from template and replace {mount_path}
        description = metadata.description
        if template.raw_content:
            import re

            desc_match = re.search(r"description:\s*(.+?)(?:\n|$)", template.raw_content)
            if desc_match:
                description = desc_match.group(1).strip().replace("{mount_path}", mount_path)

        # Build frontmatter
        frontmatter: dict[str, Any] = {
            "name": metadata.name,
            "display_name": metadata.display_name,
            "description": description,
            "version": metadata.version,
            "skill_type": "service" if metadata.connector else "mcp_tools",
            "capabilities": metadata.capabilities,
        }

        if metadata.connector:
            frontmatter["connector"] = metadata.connector
        if metadata.mcp_name:
            frontmatter["mcp"] = metadata.mcp_name
        if metadata.oauth_provider:
            frontmatter["oauth_provider"] = metadata.oauth_provider
        if metadata.tool_count > 0:
            frontmatter["tool_count"] = metadata.tool_count

        frontmatter_yaml = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False)

        # Add MCP tools section if available
        tools_section = ""
        if tool_defs:
            tools_section = self._generate_tools_section(tool_defs)
        elif mcp_tools:
            tools_section = self._generate_tools_section_from_dicts(mcp_tools)

        if tools_section:
            content = content.rstrip() + "\n\n" + tools_section

        return f"---\n{frontmatter_yaml}---\n\n{content}"

    def _generate_mcp_only(
        self,
        mount_path: str,
        metadata: SkillMetadata,
        mcp_tools: list[dict[str, Any]] | None,
        tool_defs: list[MCPToolDefinition] | None,
        mcp_mount: MCPMount | None,
    ) -> str:
        """Generate SKILL.md for MCP-only service."""
        # Replace {mount_path} in description
        description = metadata.description.replace("{mount_path}", mount_path)

        # Build frontmatter
        frontmatter: dict[str, Any] = {
            "name": metadata.name,
            "description": description,
            "version": metadata.version,
            "skill_type": "mcp_tools",
            "tool_count": len(tool_defs) if tool_defs else len(mcp_tools or []),
        }

        if metadata.transport:
            frontmatter["transport"] = metadata.transport
        if mcp_mount and mcp_mount.command:
            frontmatter["command"] = mcp_mount.command
        if mcp_mount and mcp_mount.url:
            frontmatter["url"] = mcp_mount.url

        frontmatter_yaml = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False)

        # Build content
        content_parts = [
            f"# {metadata.display_name}",
            "",
            description,
            "",
            f"**Mount Path:** `{mount_path}`",
            f"**Tools:** {frontmatter['tool_count']}",
        ]

        if metadata.transport:
            content_parts.append(f"**Transport:** {metadata.transport}")

        content_parts.append("")

        # Add tools section
        if tool_defs:
            content_parts.append(self._generate_tools_section(tool_defs))
        elif mcp_tools:
            content_parts.append(self._generate_tools_section_from_dicts(mcp_tools))

        content = "\n".join(content_parts)
        return f"---\n{frontmatter_yaml}---\n\n{content}"

    def _generate_basic(self, mount_path: str, metadata: SkillMetadata) -> str:
        """Generate basic SKILL.md (no MCP tools, no template)."""
        frontmatter = {
            "name": metadata.name,
            "description": metadata.description,
            "version": metadata.version,
            "skill_type": "service",
            "capabilities": metadata.capabilities,
        }

        if metadata.connector:
            frontmatter["connector"] = metadata.connector
        if metadata.oauth_provider:
            frontmatter["oauth_provider"] = metadata.oauth_provider

        frontmatter_yaml = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False)

        content_parts = [
            f"# {metadata.display_name}",
            "",
            metadata.description,
            "",
            f"**Mount Path:** `{mount_path}`",
            "",
        ]

        if metadata.capabilities:
            content_parts.append("## Capabilities")
            content_parts.append("")
            for cap in metadata.capabilities:
                content_parts.append(f"- {cap}")
            content_parts.append("")

        content = "\n".join(content_parts)
        return f"---\n{frontmatter_yaml}---\n\n{content}"

    def _generate_tools_section(self, tool_defs: list[MCPToolDefinition]) -> str:
        """Generate markdown section for MCP tools."""
        parts = ["## MCP Tools", ""]

        for tool_def in sorted(tool_defs, key=lambda t: t.name):
            parts.append(f"### {tool_def.name}")
            parts.append("")
            parts.append(tool_def.description)
            parts.append("")

            # Add input parameters
            if tool_def.mcp_config and tool_def.mcp_config.input_schema:
                schema = tool_def.mcp_config.input_schema
                props = schema.get("properties", {})
                required = schema.get("required", [])

                if props:
                    parts.append("**Parameters:**")
                    for param_name, param_info in props.items():
                        param_type = param_info.get("type", "any")
                        param_desc = param_info.get("description", "")
                        req_mark = " *(required)*" if param_name in required else ""
                        parts.append(f"- `{param_name}` ({param_type}){req_mark}: {param_desc}")
                    parts.append("")

        return "\n".join(parts)

    def _generate_tools_section_from_dicts(self, tools: list[dict[str, Any]]) -> str:
        """Generate markdown section from tool dictionaries."""
        parts = ["## MCP Tools", ""]

        for tool in sorted(tools, key=lambda t: t.get("name", "")):
            name = tool.get("name", "unknown")
            description = tool.get("description", "")
            input_schema = tool.get("inputSchema", {})

            parts.append(f"### {name}")
            parts.append("")
            parts.append(description)
            parts.append("")

            # Add input parameters
            props = input_schema.get("properties", {})
            required = input_schema.get("required", [])

            if props:
                parts.append("**Parameters:**")
                for param_name, param_info in props.items():
                    param_type = param_info.get("type", "any")
                    param_desc = param_info.get("description", "")
                    req_mark = " *(required)*" if param_name in required else ""
                    parts.append(f"- `{param_name}` ({param_type}){req_mark}: {param_desc}")
                parts.append("")

        return "\n".join(parts)


# Module-level generator instance for convenience
_generator: SkillGenerator | None = None


def get_skill_generator() -> SkillGenerator:
    """Get the shared skill generator instance."""
    global _generator
    if _generator is None:
        _generator = SkillGenerator()
    return _generator


def generate_skill_md(
    service_name: str,
    mount_path: str,
    mcp_tools: list[dict[str, Any]] | None = None,
    mcp_mount: MCPMount | None = None,
    tool_defs: list[MCPToolDefinition] | None = None,
) -> str:
    """Convenience function to generate SKILL.md content.

    Args:
        service_name: Unified service name
        mount_path: Mount path for the service
        mcp_tools: Optional list of MCP tool dicts
        mcp_mount: Optional MCPMount for metadata
        tool_defs: Optional list of MCPToolDefinition

    Returns:
        Generated SKILL.md content
    """
    return get_skill_generator().generate_skill_md(
        service_name=service_name,
        mount_path=mount_path,
        mcp_tools=mcp_tools,
        mcp_mount=mcp_mount,
        tool_defs=tool_defs,
    )
