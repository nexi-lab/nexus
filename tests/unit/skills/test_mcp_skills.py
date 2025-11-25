"""Unit tests for MCP skills integration."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from nexus.core.exceptions import ValidationError
from nexus.skills.mcp_exporter import NEXUS_TOOLS, MCPToolExporter
from nexus.skills.mcp_models import (
    MCPMount,
    MCPToolConfig,
    MCPToolDefinition,
    MCPToolExample,
)
from nexus.skills.mcp_mount import MCPMountError, MCPMountManager
from nexus.skills.models import SkillMetadata

# =============================================================================
# MCPToolConfig Tests
# =============================================================================


def test_mcp_tool_config_initialization() -> None:
    """Test MCPToolConfig initialization with required fields."""
    config = MCPToolConfig(
        endpoint="nexus://tools/nexus_grep",
        input_schema={"type": "object", "properties": {"pattern": {"type": "string"}}},
    )

    assert config.endpoint == "nexus://tools/nexus_grep"
    assert config.input_schema == {"type": "object", "properties": {"pattern": {"type": "string"}}}
    assert config.output_schema == {}
    assert config.requires_mount is False
    assert config.mount_name is None
    assert config.when_to_use == ""
    assert config.related_tools == []
    assert config.examples == []
    assert config.category is None


def test_mcp_tool_config_with_all_fields() -> None:
    """Test MCPToolConfig with all optional fields."""
    examples = [
        MCPToolExample(
            use_case="Search for TODOs",
            input={"pattern": "TODO:", "path": "/workspace"},
            output={"matches": []},
            description="Find all TODO comments",
        )
    ]

    config = MCPToolConfig(
        endpoint="nexus://tools/nexus_grep",
        input_schema={"type": "object", "properties": {"pattern": {"type": "string"}}},
        output_schema={"type": "array"},
        requires_mount=True,
        mount_name="github",
        when_to_use="Use for searching file contents",
        related_tools=["nexus_glob", "nexus_read_file"],
        examples=examples,
        category="search",
    )

    assert config.requires_mount is True
    assert config.mount_name == "github"
    assert config.when_to_use == "Use for searching file contents"
    assert config.related_tools == ["nexus_glob", "nexus_read_file"]
    assert len(config.examples) == 1
    assert config.category == "search"


def test_mcp_tool_config_to_dict() -> None:
    """Test MCPToolConfig serialization to dict."""
    config = MCPToolConfig(
        endpoint="nexus://tools/nexus_grep",
        input_schema={"type": "object"},
        when_to_use="Search files",
        category="search",
    )

    result = config.to_dict()

    assert result["endpoint"] == "nexus://tools/nexus_grep"
    assert result["input_schema"] == {"type": "object"}
    assert result["when_to_use"] == "Search files"
    assert result["category"] == "search"


def test_mcp_tool_config_from_dict() -> None:
    """Test MCPToolConfig deserialization from dict."""
    data = {
        "endpoint": "nexus://tools/nexus_grep",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "string"},
        "requires_mount": True,
        "mount_name": "github",
        "when_to_use": "Search files",
        "related_tools": ["nexus_glob"],
        "examples": [{"use_case": "Test", "input": {"pattern": "TODO"}, "output": {"matches": []}}],
        "category": "search",
    }

    config = MCPToolConfig.from_dict(data)

    assert config.endpoint == "nexus://tools/nexus_grep"
    assert config.requires_mount is True
    assert config.mount_name == "github"
    assert len(config.examples) == 1
    assert config.examples[0].use_case == "Test"


# =============================================================================
# MCPMount Tests
# =============================================================================


def test_mcp_mount_initialization() -> None:
    """Test MCPMount initialization with required fields."""
    mount = MCPMount(
        name="github",
        description="GitHub API integration",
        transport="stdio",
    )

    assert mount.name == "github"
    assert mount.description == "GitHub API integration"
    assert mount.transport == "stdio"
    assert mount.command is None
    assert mount.url is None
    assert mount.args == []
    assert mount.env == {}
    assert mount.mounted is False
    assert mount.tool_count == 0


def test_mcp_mount_with_all_fields() -> None:
    """Test MCPMount with all optional fields."""
    now = datetime.now(UTC)

    mount = MCPMount(
        name="github",
        description="GitHub API integration",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        env={"GITHUB_TOKEN": "xxx"},
        auth_type="oauth",
        auth_config={"client_id": "abc"},
        tools_path="/skills/system/mcp-tools/github/",
        mounted=True,
        mounted_at=now,
        last_sync=now,
        tool_count=15,
        tools=["create_issue", "list_repos"],
    )

    assert mount.command == "npx"
    assert mount.args == ["-y", "@modelcontextprotocol/server-github"]
    assert mount.env == {"GITHUB_TOKEN": "xxx"}
    assert mount.auth_type == "oauth"
    assert mount.mounted is True
    assert mount.tool_count == 15
    assert mount.tools == ["create_issue", "list_repos"]


def test_mcp_mount_to_dict() -> None:
    """Test MCPMount serialization to dict."""
    mount = MCPMount(
        name="github",
        description="GitHub API",
        transport="stdio",
        command="npx",
        tool_count=10,
    )

    result = mount.to_dict()

    assert result["name"] == "github"
    assert result["description"] == "GitHub API"
    assert result["transport"] == "stdio"
    assert result["command"] == "npx"
    assert result["tool_count"] == 10


def test_mcp_mount_from_dict() -> None:
    """Test MCPMount deserialization from dict."""
    data = {
        "name": "github",
        "description": "GitHub API",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "mounted": False,
        "tool_count": 15,
        "tools": ["create_issue"],
    }

    mount = MCPMount.from_dict(data)

    assert mount.name == "github"
    assert mount.transport == "stdio"
    assert mount.args == ["-y", "@modelcontextprotocol/server-github"]
    assert mount.tool_count == 15
    assert mount.tools == ["create_issue"]


# =============================================================================
# MCPToolDefinition Tests
# =============================================================================


def test_mcp_tool_definition_initialization() -> None:
    """Test MCPToolDefinition initialization."""
    config = MCPToolConfig(
        endpoint="nexus://tools/nexus_grep",
        input_schema={"type": "object"},
    )

    tool_def = MCPToolDefinition(
        name="nexus_grep",
        description="Search file contents",
        mcp_config=config,
    )

    assert tool_def.name == "nexus_grep"
    assert tool_def.description == "Search file contents"
    assert tool_def.version == "1.0.0"
    assert tool_def.skill_type == "mcp_tool"
    assert tool_def.mcp_config == config


def test_mcp_tool_definition_to_dict() -> None:
    """Test MCPToolDefinition serialization."""
    config = MCPToolConfig(
        endpoint="nexus://tools/nexus_grep",
        input_schema={"type": "object"},
    )

    tool_def = MCPToolDefinition(
        name="nexus_grep",
        description="Search file contents",
        mcp_config=config,
        author="Nexus",
        tags=["search"],
    )

    result = tool_def.to_dict()

    assert result["name"] == "nexus_grep"
    assert result["description"] == "Search file contents"
    assert result["skill_type"] == "mcp_tool"
    assert result["author"] == "Nexus"
    assert result["tags"] == ["search"]
    assert "mcp_config" in result


def test_mcp_tool_definition_from_dict() -> None:
    """Test MCPToolDefinition deserialization."""
    data = {
        "name": "nexus_grep",
        "description": "Search file contents",
        "version": "2.0.0",
        "skill_type": "mcp_tool",
        "mcp_config": {
            "endpoint": "nexus://tools/nexus_grep",
            "input_schema": {"type": "object"},
        },
        "author": "Nexus",
        "tags": ["search"],
    }

    tool_def = MCPToolDefinition.from_dict(data)

    assert tool_def.name == "nexus_grep"
    assert tool_def.version == "2.0.0"
    assert tool_def.mcp_config is not None
    assert tool_def.mcp_config.endpoint == "nexus://tools/nexus_grep"


# =============================================================================
# SkillMetadata with MCP Tests
# =============================================================================


def test_skill_metadata_with_mcp_tool_type() -> None:
    """Test SkillMetadata with mcp_tool skill type."""
    config = MCPToolConfig(
        endpoint="nexus://tools/nexus_grep",
        input_schema={"type": "object"},
    )

    metadata = SkillMetadata(
        name="nexus_grep",
        description="Search file contents",
        skill_type="mcp_tool",
        mcp_config=config,
    )

    assert metadata.skill_type == "mcp_tool"
    assert metadata.mcp_config == config
    # Validation should pass
    metadata.validate()


def test_skill_metadata_mcp_tool_missing_config() -> None:
    """Test that validation fails when mcp_config is missing for mcp_tool type."""
    metadata = SkillMetadata(
        name="nexus_grep",
        description="Search file contents",
        skill_type="mcp_tool",
        mcp_config=None,
    )

    with pytest.raises(ValidationError, match="mcp_config is required"):
        metadata.validate()


def test_skill_metadata_hybrid_type() -> None:
    """Test SkillMetadata with hybrid skill type."""
    config = MCPToolConfig(
        endpoint="nexus://tools/nexus_grep",
        input_schema={"type": "object"},
    )

    metadata = SkillMetadata(
        name="nexus_grep",
        description="Search file contents",
        skill_type="hybrid",
        mcp_config=config,
    )

    assert metadata.skill_type == "hybrid"
    metadata.validate()  # Should pass


def test_skill_metadata_invalid_skill_type() -> None:
    """Test that validation fails with invalid skill_type."""
    metadata = SkillMetadata(
        name="test-skill",
        description="Test",
        skill_type="invalid_type",
    )

    with pytest.raises(ValidationError, match="skill_type must be one of"):
        metadata.validate()


def test_skill_metadata_documentation_type_default() -> None:
    """Test that documentation is the default skill type."""
    metadata = SkillMetadata(
        name="test-skill",
        description="Test",
    )

    assert metadata.skill_type == "documentation"
    metadata.validate()  # Should pass without mcp_config


# =============================================================================
# MCPToolExporter Tests
# =============================================================================


def test_mcp_exporter_nexus_tools_defined() -> None:
    """Test that NEXUS_TOOLS contains expected tools."""
    tool_names = [t["name"] for t in NEXUS_TOOLS]

    # Verify key tools are defined
    assert "nexus_read_file" in tool_names
    assert "nexus_write_file" in tool_names
    assert "nexus_grep" in tool_names
    assert "nexus_glob" in tool_names
    assert "nexus_semantic_search" in tool_names
    assert "nexus_store_memory" in tool_names
    assert "nexus_python" in tool_names


def test_mcp_exporter_tool_categories() -> None:
    """Test that tools have proper categories."""
    exporter = MCPToolExporter(filesystem=None)
    categories = exporter.get_tool_categories()

    assert "file_operations" in categories
    assert "search" in categories
    assert "memory" in categories
    assert "sandbox" in categories

    # Verify file operations tools
    assert "nexus_read_file" in categories["file_operations"]
    assert "nexus_write_file" in categories["file_operations"]

    # Verify search tools
    assert "nexus_grep" in categories["search"]
    assert "nexus_glob" in categories["search"]


def test_mcp_exporter_tool_count() -> None:
    """Test tool count methods."""
    exporter = MCPToolExporter(filesystem=None)

    total_count = exporter.get_tool_count(include_sandbox=True)
    no_sandbox_count = exporter.get_tool_count(include_sandbox=False)

    # There should be sandbox tools
    assert total_count > no_sandbox_count


def test_mcp_exporter_create_tool_definition() -> None:
    """Test creating tool definition from tool data."""
    exporter = MCPToolExporter(filesystem=None)

    tool_data = {
        "name": "test_tool",
        "description": "A test tool",
        "category": "test",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "string"},
        "when_to_use": "Use for testing",
        "examples": [{"use_case": "Test case", "input": {}}],
        "related_tools": ["other_tool"],
    }

    tool_def = exporter._create_tool_definition(tool_data)

    assert tool_def.name == "test_tool"
    assert tool_def.description == "A test tool"
    assert tool_def.skill_type == "mcp_tool"
    assert tool_def.mcp_config is not None
    assert tool_def.mcp_config.endpoint == "nexus://tools/test_tool"
    assert tool_def.mcp_config.category == "test"


def test_mcp_exporter_generate_skill_md() -> None:
    """Test SKILL.md generation from tool definition."""
    exporter = MCPToolExporter(filesystem=None)

    config = MCPToolConfig(
        endpoint="nexus://tools/test_tool",
        input_schema={"type": "object", "properties": {"arg": {"type": "string"}}},
        when_to_use="Use for testing",
        examples=[MCPToolExample(use_case="Test", input={"arg": "value"})],
        related_tools=["other_tool"],
    )

    tool_def = MCPToolDefinition(
        name="test_tool",
        description="A test tool",
        mcp_config=config,
        author="Test",
    )

    skill_md = exporter._generate_skill_md(tool_def)

    # Verify frontmatter
    assert "---" in skill_md
    assert "name: test_tool" in skill_md
    assert "skill_type: mcp_tool" in skill_md

    # Verify content sections
    assert "# test_tool" in skill_md
    assert "## Endpoint" in skill_md
    assert "nexus://tools/test_tool" in skill_md
    assert "## When to Use" in skill_md
    assert "## Input Schema" in skill_md
    assert "## Examples" in skill_md
    assert "## Related Tools" in skill_md


# =============================================================================
# MCPMountManager Tests
# =============================================================================


def test_mcp_mount_manager_initialization() -> None:
    """Test MCPMountManager initialization."""
    mock_fs = MagicMock()
    mock_fs.exists.return_value = False

    manager = MCPMountManager(filesystem=mock_fs)

    assert manager._mounts == {}
    assert manager._clients == {}


def test_mcp_mount_manager_add_mount_config() -> None:
    """Test adding mount configuration without connecting."""
    mock_fs = MagicMock()
    mock_fs.exists.return_value = False

    manager = MCPMountManager(filesystem=mock_fs)

    mount = MCPMount(
        name="github",
        description="GitHub API",
        transport="stdio",
        command="npx",
    )

    manager.add_mount_config(mount)

    assert "github" in manager._mounts
    assert manager._mounts["github"].tools_path == "/skills/system/mcp-tools/github/"
    assert manager._mounts["github"].mounted is False


def test_mcp_mount_manager_list_mounts() -> None:
    """Test listing mount configurations."""
    mock_fs = MagicMock()
    mock_fs.exists.return_value = False

    manager = MCPMountManager(filesystem=mock_fs)

    # Add some mounts
    mount1 = MCPMount(name="github", description="GitHub", transport="stdio")
    mount2 = MCPMount(name="slack", description="Slack", transport="stdio")

    manager.add_mount_config(mount1)
    manager.add_mount_config(mount2)

    all_mounts = manager.list_mounts(include_unmounted=True)
    assert len(all_mounts) == 2

    # Since none are mounted, filtering should return empty
    mounted_only = manager.list_mounts(include_unmounted=False)
    assert len(mounted_only) == 0


def test_mcp_mount_manager_get_mount() -> None:
    """Test getting mount by name."""
    mock_fs = MagicMock()
    mock_fs.exists.return_value = False

    manager = MCPMountManager(filesystem=mock_fs)

    mount = MCPMount(name="github", description="GitHub", transport="stdio")
    manager.add_mount_config(mount)

    retrieved = manager.get_mount("github")
    assert retrieved is not None
    assert retrieved.name == "github"

    not_found = manager.get_mount("nonexistent")
    assert not_found is None


def test_mcp_mount_manager_remove_mount() -> None:
    """Test removing mount configuration."""
    mock_fs = MagicMock()
    mock_fs.exists.return_value = False

    manager = MCPMountManager(filesystem=mock_fs)

    mount = MCPMount(name="github", description="GitHub", transport="stdio")
    manager.add_mount_config(mount)

    result = manager.remove_mount("github")
    assert result is True
    assert "github" not in manager._mounts

    # Try to remove non-existent mount
    result = manager.remove_mount("nonexistent")
    assert result is False


@pytest.mark.asyncio
async def test_mcp_mount_manager_mount_validation() -> None:
    """Test mount validation."""
    mock_fs = MagicMock()
    mock_fs.exists.return_value = False

    manager = MCPMountManager(filesystem=mock_fs)

    # Test missing name
    mount = MCPMount(name="", description="Test", transport="stdio")
    with pytest.raises(MCPMountError, match="Mount name is required"):
        await manager.mount(mount)

    # Test invalid transport
    mount = MCPMount(name="test", description="Test", transport="invalid")
    with pytest.raises(MCPMountError, match="Unsupported transport"):
        await manager.mount(mount)

    # Test missing command for stdio
    mount = MCPMount(name="test", description="Test", transport="stdio", command=None)
    with pytest.raises(MCPMountError, match="Command is required"):
        await manager.mount(mount)

    # Test missing URL for http
    mount = MCPMount(name="test", description="Test", transport="http", url=None)
    with pytest.raises(MCPMountError, match="URL is required"):
        await manager.mount(mount)


@pytest.mark.asyncio
async def test_mcp_mount_manager_unmount_not_found() -> None:
    """Test unmounting non-existent mount."""
    mock_fs = MagicMock()
    mock_fs.exists.return_value = False

    manager = MCPMountManager(filesystem=mock_fs)

    with pytest.raises(MCPMountError, match="Mount not found"):
        await manager.unmount("nonexistent")


# =============================================================================
# Integration Tests
# =============================================================================


@pytest.mark.asyncio
async def test_mcp_exporter_export_nexus_tools() -> None:
    """Test exporting Nexus tools to filesystem."""
    mock_fs = MagicMock()
    mock_fs.mkdir = MagicMock()
    mock_fs.write = MagicMock()

    exporter = MCPToolExporter(filesystem=mock_fs)

    # Export without sandbox tools to reduce test complexity
    count = await exporter.export_nexus_tools(include_sandbox=False)

    # Verify some tools were exported
    assert count > 0

    # Verify mkdir was called for tool directories
    assert mock_fs.mkdir.call_count > 0

    # Verify write was called (2 files per tool: tool.json and SKILL.md)
    assert mock_fs.write.call_count == count * 2
