"""MCP Tool Exporter - Export Nexus MCP tools to Skills format.

This module exports Nexus's built-in MCP tools to the /skills/system/mcp-tools/
directory, making them discoverable through the skills system.

Based on: https://www.anthropic.com/engineering/code-execution-with-mcp
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nexus.skills.mcp_models import MCPToolConfig, MCPToolDefinition, MCPToolExample

if TYPE_CHECKING:
    from nexus.skills.protocols import NexusFilesystem

logger = logging.getLogger(__name__)


# Built-in Nexus MCP tools with their documentation
NEXUS_TOOLS: list[dict[str, Any]] = [
    # File Operations
    {
        "name": "nexus_read_file",
        "description": "Read file content from Nexus filesystem",
        "category": "file_operations",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path to read (e.g., '/workspace/data.txt')",
                }
            },
            "required": ["path"],
        },
        "output_schema": {"type": "string", "description": "File content as string"},
        "when_to_use": "Use when you need to read the contents of a file. Returns the file content as a string.",
        "examples": [
            {
                "use_case": "Read a Python file",
                "input": {"path": "/workspace/main.py"},
            },
            {
                "use_case": "Read a configuration file",
                "input": {"path": "/workspace/config.json"},
            },
        ],
        "related_tools": ["nexus_write_file", "nexus_list_files", "nexus_file_info"],
    },
    {
        "name": "nexus_write_file",
        "description": "Write content to a file in Nexus filesystem",
        "category": "file_operations",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path to write (e.g., '/workspace/data.txt')",
                },
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["path", "content"],
        },
        "output_schema": {"type": "string", "description": "Success message or error"},
        "when_to_use": "Use when you need to create or update a file with new content.",
        "examples": [
            {
                "use_case": "Create a new file",
                "input": {"path": "/workspace/notes.txt", "content": "My notes..."},
            },
        ],
        "related_tools": ["nexus_read_file", "nexus_delete_file"],
    },
    {
        "name": "nexus_delete_file",
        "description": "Delete a file from Nexus filesystem",
        "category": "file_operations",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path to delete (e.g., '/workspace/data.txt')",
                }
            },
            "required": ["path"],
        },
        "output_schema": {"type": "string", "description": "Success message or error"},
        "when_to_use": "Use when you need to remove a file from the filesystem.",
        "related_tools": ["nexus_write_file", "nexus_rmdir"],
    },
    {
        "name": "nexus_list_files",
        "description": "List files in a directory with optional recursive listing and detailed metadata",
        "category": "file_operations",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path to list (default: '/')",
                    "default": "/",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "Whether to list recursively (default: false)",
                    "default": False,
                },
                "details": {
                    "type": "boolean",
                    "description": "Whether to include detailed metadata (default: true)",
                    "default": True,
                },
            },
        },
        "output_schema": {
            "type": "string",
            "description": "JSON string with list of files",
        },
        "when_to_use": "Use when you need to see what files exist in a directory. Set recursive=true to see all files in subdirectories.",
        "examples": [
            {
                "use_case": "List workspace contents",
                "input": {"path": "/workspace", "recursive": False},
            },
            {
                "use_case": "List all Python files",
                "input": {"path": "/workspace", "recursive": True},
            },
        ],
        "related_tools": ["nexus_glob", "nexus_file_info"],
    },
    {
        "name": "nexus_file_info",
        "description": "Get detailed information about a file including size and type",
        "category": "file_operations",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path to get info for",
                }
            },
            "required": ["path"],
        },
        "output_schema": {"type": "string", "description": "JSON string with file metadata"},
        "when_to_use": "Use when you need to check if a file exists and get its metadata without reading the full content.",
        "related_tools": ["nexus_list_files", "nexus_read_file"],
    },
    # Directory Operations
    {
        "name": "nexus_mkdir",
        "description": "Create a directory in Nexus filesystem",
        "category": "directory_operations",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path to create (e.g., '/workspace/data')",
                }
            },
            "required": ["path"],
        },
        "output_schema": {"type": "string", "description": "Success message or error"},
        "when_to_use": "Use when you need to create a new directory.",
        "related_tools": ["nexus_rmdir", "nexus_list_files"],
    },
    {
        "name": "nexus_rmdir",
        "description": "Remove a directory from Nexus filesystem",
        "category": "directory_operations",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path to remove (e.g., '/workspace/data')",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "Whether to remove recursively (default: false)",
                    "default": False,
                },
            },
            "required": ["path"],
        },
        "output_schema": {"type": "string", "description": "Success message or error"},
        "when_to_use": "Use when you need to remove a directory. Set recursive=true to remove non-empty directories.",
        "related_tools": ["nexus_mkdir", "nexus_delete_file"],
    },
    # Search Tools
    {
        "name": "nexus_glob",
        "description": "Search files using glob pattern (e.g., '**/*.py' for all Python files)",
        "category": "search",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern (e.g., '**/*.py', '*.txt')",
                },
                "path": {
                    "type": "string",
                    "description": "Base path to search from (default: '/')",
                    "default": "/",
                },
            },
            "required": ["pattern"],
        },
        "output_schema": {
            "type": "string",
            "description": "JSON string with list of matching file paths",
        },
        "when_to_use": "Use when you need to find files by name pattern. Supports wildcards like * and ** for recursive matching.",
        "examples": [
            {
                "use_case": "Find all Python files",
                "input": {"pattern": "**/*.py", "path": "/workspace"},
            },
            {
                "use_case": "Find all config files",
                "input": {"pattern": "**/*.{json,yaml,yml}", "path": "/"},
            },
        ],
        "related_tools": ["nexus_grep", "nexus_list_files"],
    },
    {
        "name": "nexus_grep",
        "description": "Search file contents using regex pattern, returns matching lines with context",
        "category": "search",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for",
                },
                "path": {
                    "type": "string",
                    "description": "Base path to search from (default: '/')",
                    "default": "/",
                },
                "ignore_case": {
                    "type": "boolean",
                    "description": "Whether to ignore case (default: false)",
                    "default": False,
                },
            },
            "required": ["pattern"],
        },
        "output_schema": {
            "type": "string",
            "description": "JSON string with search results (file paths, line numbers, content)",
        },
        "when_to_use": "Use when you need to find specific content within files. Great for finding function definitions, error messages, or any text pattern.",
        "examples": [
            {
                "use_case": "Find all TODO comments",
                "input": {"pattern": "TODO:", "path": "/workspace", "ignore_case": True},
            },
            {
                "use_case": "Find function definitions",
                "input": {"pattern": "def \\w+\\(", "path": "/workspace"},
            },
        ],
        "related_tools": ["nexus_glob", "nexus_semantic_search"],
    },
    {
        "name": "nexus_semantic_search",
        "description": "Search files semantically using natural language query (AI-powered)",
        "category": "search",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results (default: 10)",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
        "output_schema": {"type": "string", "description": "JSON string with search results"},
        "when_to_use": "Use when you need to find files based on meaning rather than exact text. Best for conceptual searches like 'authentication logic' or 'error handling code'.",
        "examples": [
            {
                "use_case": "Find authentication code",
                "input": {"query": "user authentication and login logic", "limit": 5},
            },
            {
                "use_case": "Find API endpoints",
                "input": {"query": "REST API route handlers", "limit": 10},
            },
        ],
        "related_tools": ["nexus_grep", "nexus_glob"],
    },
    # Memory Tools
    {
        "name": "nexus_store_memory",
        "description": "Store a memory for later retrieval using semantic search",
        "category": "memory",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Memory content to store",
                },
                "memory_type": {
                    "type": "string",
                    "description": "Optional memory type/category",
                },
                "importance": {
                    "type": "number",
                    "description": "Importance score 0.0-1.0 (default: 0.5)",
                    "default": 0.5,
                },
            },
            "required": ["content"],
        },
        "output_schema": {"type": "string", "description": "Success message or error"},
        "when_to_use": "Use when you learn something important that should be remembered for future conversations. Store insights, decisions, user preferences, or key findings.",
        "examples": [
            {
                "use_case": "Store a user preference",
                "input": {
                    "content": "User prefers Python over JavaScript",
                    "memory_type": "preference",
                    "importance": 0.8,
                },
            },
            {
                "use_case": "Store a project insight",
                "input": {
                    "content": "The authentication system uses JWT tokens",
                    "memory_type": "project_knowledge",
                    "importance": 0.7,
                },
            },
        ],
        "related_tools": ["nexus_query_memory"],
    },
    {
        "name": "nexus_query_memory",
        "description": "Query stored memories using semantic search",
        "category": "memory",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query",
                },
                "memory_type": {
                    "type": "string",
                    "description": "Optional filter by memory type",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results (default: 5)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
        "output_schema": {
            "type": "string",
            "description": "JSON string with matching memories",
        },
        "when_to_use": "Use when you need to recall previously stored information. Query memories to retrieve past decisions, user preferences, or project knowledge.",
        "examples": [
            {
                "use_case": "Recall user preferences",
                "input": {
                    "query": "programming language preference",
                    "memory_type": "preference",
                },
            },
        ],
        "related_tools": ["nexus_store_memory"],
    },
    # Workflow Tools
    {
        "name": "nexus_list_workflows",
        "description": "List available workflows in Nexus",
        "category": "workflow",
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {
            "type": "string",
            "description": "JSON string with list of workflows",
        },
        "when_to_use": "Use when you need to see what pre-defined workflows are available for execution.",
        "related_tools": ["nexus_execute_workflow"],
    },
    {
        "name": "nexus_execute_workflow",
        "description": "Execute a workflow by name with provided inputs",
        "category": "workflow",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Workflow name",
                },
                "inputs": {
                    "type": "string",
                    "description": "Optional JSON string with workflow inputs",
                },
            },
            "required": ["name"],
        },
        "output_schema": {"type": "string", "description": "Workflow execution result"},
        "when_to_use": "Use when you need to run a pre-defined workflow. Workflows can automate complex multi-step operations.",
        "related_tools": ["nexus_list_workflows"],
    },
    # Sandbox Tools (conditionally available)
    {
        "name": "nexus_python",
        "description": "Execute Python code in an isolated sandbox environment",
        "category": "sandbox",
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute",
                },
                "sandbox_id": {
                    "type": "string",
                    "description": "Sandbox ID (use nexus_sandbox_create to create one)",
                },
            },
            "required": ["code", "sandbox_id"],
        },
        "output_schema": {
            "type": "string",
            "description": "Execution result with stdout, stderr, exit_code, and execution time",
        },
        "when_to_use": "Use when you need to execute Python code safely. The sandbox provides isolation for running untrusted code.",
        "examples": [
            {
                "use_case": "Run a calculation",
                "input": {
                    "code": "result = sum(range(100))\nprint(f'Sum: {result}')",
                    "sandbox_id": "sandbox-123",
                },
            },
        ],
        "related_tools": [
            "nexus_bash",
            "nexus_sandbox_create",
            "nexus_sandbox_list",
            "nexus_sandbox_stop",
        ],
    },
    {
        "name": "nexus_bash",
        "description": "Execute bash commands in an isolated sandbox environment",
        "category": "sandbox",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Bash command to execute",
                },
                "sandbox_id": {
                    "type": "string",
                    "description": "Sandbox ID (use nexus_sandbox_create to create one)",
                },
            },
            "required": ["command", "sandbox_id"],
        },
        "output_schema": {
            "type": "string",
            "description": "Execution result with stdout, stderr, exit_code, and execution time",
        },
        "when_to_use": "Use when you need to run shell commands safely. The sandbox provides isolation for running system commands.",
        "related_tools": ["nexus_python", "nexus_sandbox_create"],
    },
    {
        "name": "nexus_sandbox_create",
        "description": "Create a new sandbox for code execution",
        "category": "sandbox",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "User-friendly sandbox name",
                },
                "ttl_minutes": {
                    "type": "integer",
                    "description": "Idle timeout in minutes (default: 10)",
                    "default": 10,
                },
            },
            "required": ["name"],
        },
        "output_schema": {
            "type": "string",
            "description": "JSON string with sandbox_id and metadata",
        },
        "when_to_use": "Use before running Python or bash code. Creates an isolated environment for safe code execution.",
        "related_tools": ["nexus_python", "nexus_bash", "nexus_sandbox_stop"],
    },
    {
        "name": "nexus_sandbox_list",
        "description": "List all active sandboxes",
        "category": "sandbox",
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {
            "type": "string",
            "description": "JSON string with list of sandboxes",
        },
        "when_to_use": "Use to see what sandboxes are currently active and available for code execution.",
        "related_tools": ["nexus_sandbox_create", "nexus_sandbox_stop"],
    },
    {
        "name": "nexus_sandbox_stop",
        "description": "Stop and destroy a sandbox",
        "category": "sandbox",
        "input_schema": {
            "type": "object",
            "properties": {
                "sandbox_id": {
                    "type": "string",
                    "description": "Sandbox ID to stop",
                }
            },
            "required": ["sandbox_id"],
        },
        "output_schema": {"type": "string", "description": "Success message or error"},
        "when_to_use": "Use when you're done with a sandbox to clean up resources.",
        "related_tools": ["nexus_sandbox_create", "nexus_sandbox_list"],
    },
]


class MCPToolExporter:
    """Export Nexus MCP tools to Skills format.

    This exporter creates SKILL.md and tool.json files for each
    built-in Nexus MCP tool, making them discoverable through
    the skills system.

    Example:
        >>> from nexus import connect
        >>> from nexus.skills.mcp_exporter import MCPToolExporter
        >>>
        >>> nx = connect()
        >>> exporter = MCPToolExporter(nx)
        >>> await exporter.export_nexus_tools()
    """

    # Output path for exported tools
    OUTPUT_PATH = "/skills/system/mcp-tools/nexus/"

    def __init__(self, filesystem: NexusFilesystem | None = None):
        """Initialize exporter.

        Args:
            filesystem: Optional filesystem instance (defaults to local FS)
        """
        self._filesystem = filesystem

    async def export_nexus_tools(
        self, output_path: str | None = None, include_sandbox: bool = True
    ) -> int:
        """Export all Nexus MCP tools to skills format.

        Args:
            output_path: Custom output path (default: /skills/system/mcp-tools/nexus/)
            include_sandbox: Whether to include sandbox tools (default: true)

        Returns:
            Number of tools exported
        """
        output_path = output_path or self.OUTPUT_PATH
        exported = 0

        for tool_data in NEXUS_TOOLS:
            # Skip sandbox tools if not requested
            if not include_sandbox and tool_data.get("category") == "sandbox":
                continue

            try:
                tool_def = self._create_tool_definition(tool_data)
                await self._export_tool(tool_def, output_path)
                exported += 1
                logger.debug(f"Exported tool: {tool_def.name}")
            except Exception as e:
                logger.warning(f"Failed to export tool {tool_data.get('name')}: {e}")

        logger.info(f"Exported {exported} Nexus MCP tools to {output_path}")
        return exported

    def _create_tool_definition(self, tool_data: dict[str, Any]) -> MCPToolDefinition:
        """Create a tool definition from tool data.

        Args:
            tool_data: Tool configuration data

        Returns:
            MCPToolDefinition instance
        """
        # Create examples
        examples = []
        for ex_data in tool_data.get("examples", []):
            examples.append(
                MCPToolExample(
                    use_case=ex_data.get("use_case", ""),
                    input=ex_data.get("input", {}),
                    output=ex_data.get("output"),
                    description=ex_data.get("description"),
                )
            )

        # Create MCP config
        mcp_config = MCPToolConfig(
            endpoint=f"nexus://tools/{tool_data['name']}",
            input_schema=tool_data.get("input_schema", {}),
            output_schema=tool_data.get("output_schema", {}),
            requires_mount=False,
            mount_name=None,
            when_to_use=tool_data.get("when_to_use", ""),
            related_tools=tool_data.get("related_tools", []),
            examples=examples,
            category=tool_data.get("category"),
        )

        now = datetime.now(UTC)

        return MCPToolDefinition(
            name=tool_data["name"],
            description=tool_data.get("description", ""),
            version="1.0.0",
            skill_type="mcp_tool",
            mcp_config=mcp_config,
            author="Nexus",
            tags=[tool_data.get("category", "tool")],
            created_at=now,
            modified_at=now,
        )

    async def _export_tool(self, tool_def: MCPToolDefinition, output_path: str) -> str:
        """Export a single tool to the filesystem.

        Creates both tool.json and SKILL.md files.

        Args:
            tool_def: Tool definition
            output_path: Base output path

        Returns:
            Path to exported tool directory
        """
        tool_dir = f"{output_path}{tool_def.name}/"
        tool_json_path = f"{tool_dir}tool.json"
        skill_md_path = f"{tool_dir}SKILL.md"

        # Create tool.json
        tool_json = json.dumps(tool_def.to_dict(), indent=2)

        # Create SKILL.md
        skill_md = self._generate_skill_md(tool_def)

        if self._filesystem:
            # Create directory
            with contextlib.suppress(Exception):
                self._filesystem.mkdir(tool_dir, parents=True)

            # Write files
            self._filesystem.write(tool_json_path, tool_json.encode("utf-8"))
            self._filesystem.write(skill_md_path, skill_md.encode("utf-8"))
        else:
            # Local filesystem
            tool_dir_path = Path(tool_dir.lstrip("/"))
            tool_dir_path.mkdir(parents=True, exist_ok=True)
            (tool_dir_path / "tool.json").write_text(tool_json)
            (tool_dir_path / "SKILL.md").write_text(skill_md)

        return tool_dir

    def _generate_skill_md(self, tool_def: MCPToolDefinition) -> str:
        """Generate SKILL.md content for a tool.

        Args:
            tool_def: Tool definition

        Returns:
            SKILL.md content
        """
        import yaml

        # Build frontmatter
        frontmatter: dict[str, Any] = {
            "name": tool_def.name,
            "description": tool_def.description,
            "version": tool_def.version,
            "skill_type": tool_def.skill_type,
        }

        if tool_def.author:
            frontmatter["author"] = tool_def.author
        if tool_def.tags:
            frontmatter["tags"] = tool_def.tags
        if tool_def.mcp_config and tool_def.mcp_config.category:
            frontmatter["category"] = tool_def.mcp_config.category
        if tool_def.created_at:
            frontmatter["created_at"] = tool_def.created_at.isoformat()
        if tool_def.modified_at:
            frontmatter["modified_at"] = tool_def.modified_at.isoformat()

        frontmatter_yaml = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False)

        # Build markdown content
        content_parts = [f"# {tool_def.name}", "", tool_def.description, ""]

        # Add endpoint info
        if tool_def.mcp_config:
            content_parts.extend(
                [
                    "## Endpoint",
                    "",
                    f"`{tool_def.mcp_config.endpoint}`",
                    "",
                ]
            )

        # Add usage guidance
        if tool_def.mcp_config and tool_def.mcp_config.when_to_use:
            content_parts.extend(["## When to Use", "", tool_def.mcp_config.when_to_use, ""])

        # Add input schema
        if tool_def.mcp_config and tool_def.mcp_config.input_schema:
            content_parts.extend(
                [
                    "## Input Schema",
                    "",
                    "```json",
                    json.dumps(tool_def.mcp_config.input_schema, indent=2),
                    "```",
                    "",
                ]
            )

        # Add output schema
        if tool_def.mcp_config and tool_def.mcp_config.output_schema:
            content_parts.extend(
                [
                    "## Output Schema",
                    "",
                    "```json",
                    json.dumps(tool_def.mcp_config.output_schema, indent=2),
                    "```",
                    "",
                ]
            )

        # Add examples
        if tool_def.mcp_config and tool_def.mcp_config.examples:
            content_parts.extend(["## Examples", ""])
            for ex in tool_def.mcp_config.examples:
                content_parts.extend(
                    [
                        f"### {ex.use_case}",
                        "",
                        "**Input:**",
                        "```json",
                        json.dumps(ex.input, indent=2),
                        "```",
                        "",
                    ]
                )
                if ex.output:
                    content_parts.extend(
                        [
                            "**Output:**",
                            "```json",
                            json.dumps(ex.output, indent=2),
                            "```",
                            "",
                        ]
                    )

        # Add related tools
        if tool_def.mcp_config and tool_def.mcp_config.related_tools:
            content_parts.extend(
                [
                    "## Related Tools",
                    "",
                ]
            )
            for related in tool_def.mcp_config.related_tools:
                content_parts.append(f"- `{related}`")
            content_parts.append("")

        content = "\n".join(content_parts)

        return f"---\n{frontmatter_yaml}---\n\n{content}"

    def get_tool_categories(self) -> dict[str, list[str]]:
        """Get tools organized by category.

        Returns:
            Dictionary mapping category to list of tool names
        """
        categories: dict[str, list[str]] = {}
        for tool_data in NEXUS_TOOLS:
            category = tool_data.get("category", "other")
            if category not in categories:
                categories[category] = []
            categories[category].append(tool_data["name"])
        return categories

    def get_tool_count(self, include_sandbox: bool = True) -> int:
        """Get total number of tools.

        Args:
            include_sandbox: Whether to include sandbox tools

        Returns:
            Number of tools
        """
        if include_sandbox:
            return len(NEXUS_TOOLS)
        else:
            return len([t for t in NEXUS_TOOLS if t.get("category") != "sandbox"])
