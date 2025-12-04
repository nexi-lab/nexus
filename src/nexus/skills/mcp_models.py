"""MCP Tool and Mount models for the Skills System.

This module provides data models for integrating MCP tools with the Nexus Skills system,
enabling dynamic tool discovery and external MCP server mounting.

Based on: https://www.anthropic.com/engineering/code-execution-with-mcp
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class MCPToolExample:
    """Example usage for an MCP tool."""

    use_case: str
    input: dict[str, Any]
    output: dict[str, Any] | None = None
    description: str | None = None


@dataclass
class MCPToolConfig:
    """Configuration for MCP tool skills.

    This defines how an MCP tool is exposed as a skill, including its
    endpoint, schema, and usage documentation.

    Example:
        >>> config = MCPToolConfig(
        ...     endpoint="nexus://tools/nexus_grep",
        ...     input_schema={
        ...         "type": "object",
        ...         "properties": {
        ...             "pattern": {"type": "string"},
        ...             "path": {"type": "string"}
        ...         },
        ...         "required": ["pattern"]
        ...     },
        ...     output_schema={"type": "string"},
        ...     when_to_use="Search file contents using regex patterns",
        ... )
    """

    # Tool endpoint - identifies where the tool is located
    # Format: "nexus://tools/{tool_name}" for built-in tools
    #         "mcp://{mount_name}/{tool_name}" for mounted external MCPs
    endpoint: str

    # JSON Schema for tool inputs
    input_schema: dict[str, Any]

    # JSON Schema for tool outputs (optional)
    output_schema: dict[str, Any] = field(default_factory=dict)

    # Whether this tool requires a mount to be active
    requires_mount: bool = False

    # Name of the mount this tool belongs to (if external)
    mount_name: str | None = None

    # Usage guidance
    when_to_use: str = ""

    # Related tools for better discovery
    related_tools: list[str] = field(default_factory=list)

    # Example usages
    examples: list[MCPToolExample] = field(default_factory=list)

    # Tool category for organization
    category: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        result: dict[str, Any] = {
            "endpoint": self.endpoint,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "requires_mount": self.requires_mount,
        }

        if self.mount_name:
            result["mount_name"] = self.mount_name
        if self.when_to_use:
            result["when_to_use"] = self.when_to_use
        if self.related_tools:
            result["related_tools"] = self.related_tools
        if self.examples:
            result["examples"] = [
                {
                    "use_case": ex.use_case,
                    "input": ex.input,
                    **({"output": ex.output} if ex.output else {}),
                    **({"description": ex.description} if ex.description else {}),
                }
                for ex in self.examples
            ]
        if self.category:
            result["category"] = self.category

        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MCPToolConfig:
        """Create from dictionary."""
        examples = []
        for ex_data in data.get("examples", []):
            examples.append(
                MCPToolExample(
                    use_case=ex_data.get("use_case", ""),
                    input=ex_data.get("input", {}),
                    output=ex_data.get("output"),
                    description=ex_data.get("description"),
                )
            )

        return cls(
            endpoint=data.get("endpoint", ""),
            input_schema=data.get("input_schema", {}),
            output_schema=data.get("output_schema", {}),
            requires_mount=data.get("requires_mount", False),
            mount_name=data.get("mount_name"),
            when_to_use=data.get("when_to_use", ""),
            related_tools=data.get("related_tools", []),
            examples=examples,
            category=data.get("category"),
        )


@dataclass
class MCPMount:
    """Configuration for mounted external MCP servers.

    An MCP mount represents a connection to an external MCP server
    (e.g., GitHub, Slack, Notion) that provides additional tools.

    Example:
        >>> mount = MCPMount(
        ...     name="github",
        ...     description="GitHub API integration",
        ...     transport="stdio",
        ...     command="npx -y @modelcontextprotocol/server-github",
        ...     auth_type="oauth",
        ... )
    """

    # Mount identification
    name: str
    description: str

    # Connection configuration
    transport: str  # "stdio" | "http" | "sse" | "klavis_rest"
    command: str | None = None  # For stdio transport
    url: str | None = None  # For http/sse/klavis_rest transport
    args: list[str] = field(default_factory=list)  # Command arguments
    env: dict[str, str] = field(default_factory=dict)  # Environment variables
    headers: dict[str, str] = field(default_factory=dict)  # HTTP headers for http/sse

    # Klavis-specific configuration (for klavis_rest transport)
    klavis_api_key: str | None = None  # Klavis API key (or from env KLAVIS_API_KEY)
    klavis_strata_id: str | None = None  # Strata server ID
    klavis_connection_type: str = "StreamableHttp"  # "SSE" or "StreamableHttp"

    # Authentication
    auth_type: str | None = None  # "oauth" | "api_key" | "none"
    auth_config: dict[str, Any] = field(default_factory=dict)

    # Tool storage path
    tools_path: str | None = None  # /skills/system/mcp-tools/{name}/

    # Mount state
    mounted: bool = False
    mounted_at: datetime | None = None
    last_sync: datetime | None = None

    # Discovered tools
    tool_count: int = 0
    tools: list[str] = field(default_factory=list)  # List of tool names

    # Tier info (set during discovery)
    tier: str | None = None  # "user" | "tenant" | "system"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        result: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "transport": self.transport,
            "mounted": self.mounted,
            "tool_count": self.tool_count,
        }

        if self.command:
            result["command"] = self.command
        if self.url:
            result["url"] = self.url
        if self.args:
            result["args"] = self.args
        if self.env:
            result["env"] = self.env
        if self.headers:
            result["headers"] = self.headers
        if self.klavis_api_key:
            result["klavis_api_key"] = self.klavis_api_key
        if self.klavis_strata_id:
            result["klavis_strata_id"] = self.klavis_strata_id
        if self.klavis_connection_type and self.klavis_connection_type != "StreamableHttp":
            result["klavis_connection_type"] = self.klavis_connection_type
        if self.auth_type:
            result["auth_type"] = self.auth_type
        if self.auth_config:
            result["auth_config"] = self.auth_config
        if self.tools_path:
            result["tools_path"] = self.tools_path
        if self.mounted_at:
            result["mounted_at"] = self.mounted_at.isoformat()
        if self.last_sync:
            result["last_sync"] = self.last_sync.isoformat()
        if self.tools:
            result["tools"] = self.tools
        if self.tier:
            result["tier"] = self.tier

        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MCPMount:
        """Create from dictionary."""
        mounted_at = None
        if data.get("mounted_at"):
            mounted_at = datetime.fromisoformat(data["mounted_at"])

        last_sync = None
        if data.get("last_sync"):
            last_sync = datetime.fromisoformat(data["last_sync"])

        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            transport=data.get("transport", "stdio"),
            command=data.get("command"),
            url=data.get("url"),
            args=data.get("args", []),
            env=data.get("env", {}),
            headers=data.get("headers", {}),
            klavis_api_key=data.get("klavis_api_key"),
            klavis_strata_id=data.get("klavis_strata_id"),
            klavis_connection_type=data.get("klavis_connection_type", "StreamableHttp"),
            auth_type=data.get("auth_type"),
            auth_config=data.get("auth_config", {}),
            tools_path=data.get("tools_path"),
            mounted=data.get("mounted", False),
            mounted_at=mounted_at,
            last_sync=last_sync,
            tool_count=data.get("tool_count", 0),
            tools=data.get("tools", []),
            tier=data.get("tier"),
        )


@dataclass
class MCPToolDefinition:
    """Complete MCP tool definition stored as a skill.

    This combines the tool metadata with its MCP configuration for
    storage in the /skills/system/mcp-tools/ directory.

    Example tool.json:
        {
            "name": "nexus_grep",
            "description": "Search file contents using regex pattern",
            "version": "1.0.0",
            "skill_type": "mcp_tool",
            "mcp_config": {
                "endpoint": "nexus://tools/nexus_grep",
                "input_schema": {...},
                ...
            }
        }
    """

    # Tool identification
    name: str
    description: str
    version: str = "1.0.0"

    # Skill type indicator
    skill_type: str = "mcp_tool"  # "documentation" | "mcp_tool" | "hybrid"

    # MCP configuration
    mcp_config: MCPToolConfig | None = None

    # Additional metadata
    author: str | None = None
    tags: list[str] = field(default_factory=list)
    created_at: datetime | None = None
    modified_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "skill_type": self.skill_type,
        }

        if self.mcp_config:
            result["mcp_config"] = self.mcp_config.to_dict()
        if self.author:
            result["author"] = self.author
        if self.tags:
            result["tags"] = self.tags
        if self.created_at:
            result["created_at"] = self.created_at.isoformat()
        if self.modified_at:
            result["modified_at"] = self.modified_at.isoformat()

        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MCPToolDefinition:
        """Create from dictionary."""
        mcp_config = None
        if data.get("mcp_config"):
            mcp_config = MCPToolConfig.from_dict(data["mcp_config"])

        created_at = None
        if data.get("created_at"):
            created_at = datetime.fromisoformat(data["created_at"])

        modified_at = None
        if data.get("modified_at"):
            modified_at = datetime.fromisoformat(data["modified_at"])

        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            version=data.get("version", "1.0.0"),
            skill_type=data.get("skill_type", "mcp_tool"),
            mcp_config=mcp_config,
            author=data.get("author"),
            tags=data.get("tags", []),
            created_at=created_at,
            modified_at=modified_at,
        )
