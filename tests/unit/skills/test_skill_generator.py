"""Tests for skill_generator module."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nexus.backends.service_map import SERVICE_REGISTRY, ServiceInfo
from nexus.skills.mcp_models import MCPMount, MCPToolConfig, MCPToolDefinition
from nexus.skills.skill_generator import (
    ConnectorTemplate,
    SkillGenerator,
    SkillMetadata,
    generate_skill_md,
    get_skill_generator,
)


@pytest.fixture
def temp_templates_dir(tmp_path: Path) -> Path:
    """Create a temporary templates directory with sample templates."""
    templates_dir = tmp_path / "connector-skills"
    templates_dir.mkdir()

    # Create gdrive template
    gdrive_template = """---
name: google-drive
description: Google Drive connector mounted at {mount_path}. Access Google Drive files.
---

- **Google Docs**: Exported as DOCX by default
- **Google Sheets**: Exported as XLSX by default
- **Delete**: Moves files to Google Drive trash
"""
    (templates_dir / "gdrive_connector.md").write_text(gdrive_template)

    # Create GCS template
    gcs_template = """---
name: gcs
description: Google Cloud Storage connector mounted at {mount_path}.
---

Access GCS bucket objects through the Nexus filesystem.
"""
    (templates_dir / "gcs_connector.md").write_text(gcs_template)

    # Create template with missing frontmatter
    (templates_dir / "invalid.md").write_text("No frontmatter here")

    # Create template with invalid YAML
    invalid_yaml = """---
name: test
invalid: yaml: structure:
---
"""
    (templates_dir / "invalid_yaml.md").write_text(invalid_yaml)

    return templates_dir


@pytest.fixture
def skill_generator(temp_templates_dir: Path) -> SkillGenerator:
    """Create a SkillGenerator with temporary templates."""
    return SkillGenerator(templates_path=temp_templates_dir)


@pytest.fixture
def mock_service_registry():
    """Mock the service registry."""
    original_registry = SERVICE_REGISTRY.copy()

    # Add test service info
    SERVICE_REGISTRY["google-drive"] = ServiceInfo(
        name="google-drive",
        display_name="Google Drive",
        connector="gdrive_connector",
        klavis_mcp="google_drive",
        oauth_provider="google",
        capabilities=["read", "write", "list", "delete"],
        description="Google Drive integration",
    )

    SERVICE_REGISTRY["gcs"] = ServiceInfo(
        name="gcs",
        display_name="Google Cloud Storage",
        connector="gcs_connector",
        klavis_mcp=None,
        oauth_provider=None,
        capabilities=["read", "write", "list", "delete"],
        description="GCS integration",
    )

    yield SERVICE_REGISTRY

    # Restore original registry
    SERVICE_REGISTRY.clear()
    SERVICE_REGISTRY.update(original_registry)


class TestSkillGenerator:
    """Tests for SkillGenerator class."""

    def test_init_with_custom_path(self, temp_templates_dir: Path):
        """Test initialization with custom templates path."""
        generator = SkillGenerator(templates_path=temp_templates_dir)
        assert generator._templates_path == temp_templates_dir
        assert isinstance(generator._templates, dict)

    def test_init_with_default_path(self):
        """Test initialization with default templates path."""
        generator = SkillGenerator()
        assert generator._templates_path.name == "connector-skills"

    def test_init_nonexistent_path(self, tmp_path: Path):
        """Test initialization with nonexistent path."""
        nonexistent = tmp_path / "nonexistent"
        generator = SkillGenerator(templates_path=nonexistent)
        assert len(generator._templates) == 0

    def test_load_templates(self, skill_generator: SkillGenerator, mock_service_registry):
        """Test template loading."""
        # Should load gdrive and gcs templates (invalid ones should be skipped)
        assert len(skill_generator._templates) >= 2

        # Check template was loaded
        assert "google-drive" in skill_generator._templates
        assert "gcs" in skill_generator._templates

        # Check templates are also indexed by connector name
        assert "gdrive_connector" in skill_generator._templates
        assert "gcs_connector" in skill_generator._templates

    def test_parse_template_valid(self, temp_templates_dir: Path, mock_service_registry):
        """Test parsing valid template."""
        template_file = temp_templates_dir / "gdrive_connector.md"
        generator = SkillGenerator(templates_path=temp_templates_dir)

        template = generator._parse_template(template_file)
        assert template is not None
        assert template.name == "google-drive"
        assert template.connector == "gdrive_connector"
        assert template.oauth_provider == "google"
        assert "Google Docs" in template.content

    def test_parse_template_missing_frontmatter(self, temp_templates_dir: Path):
        """Test parsing template without frontmatter."""
        template_file = temp_templates_dir / "invalid.md"
        generator = SkillGenerator(templates_path=temp_templates_dir)

        template = generator._parse_template(template_file)
        assert template is None

    def test_parse_template_invalid_yaml(self, temp_templates_dir: Path):
        """Test parsing template with invalid YAML."""
        template_file = temp_templates_dir / "invalid_yaml.md"
        generator = SkillGenerator(templates_path=temp_templates_dir)

        template = generator._parse_template(template_file)
        assert template is None

    def test_get_template_by_service_name(
        self, skill_generator: SkillGenerator, mock_service_registry
    ):
        """Test getting template by service name."""
        template = skill_generator.get_template("google-drive")
        assert template is not None
        assert template.name == "google-drive"

    def test_get_template_by_connector_name(
        self, skill_generator: SkillGenerator, mock_service_registry
    ):
        """Test getting template by connector name."""
        template = skill_generator.get_template("gdrive_connector")
        assert template is not None
        assert template.name == "google-drive"

    def test_get_template_not_found(self, skill_generator: SkillGenerator):
        """Test getting nonexistent template."""
        template = skill_generator.get_template("nonexistent")
        assert template is None


class TestSkillGeneration:
    """Tests for skill generation methods."""

    def test_generate_skill_with_template(
        self, skill_generator: SkillGenerator, mock_service_registry
    ):
        """Test generating skill with connector template."""
        skill_md = skill_generator.generate_skill_md(
            service_name="google-drive",
            mount_path="/mnt/gdrive/user@example.com",
        )

        assert skill_md is not None
        assert "---" in skill_md  # Has frontmatter
        assert "google-drive" in skill_md
        assert "/mnt/gdrive/user@example.com" in skill_md
        assert "Google Docs" in skill_md

        # Parse frontmatter
        parts = skill_md.split("---")
        assert len(parts) >= 3
        frontmatter = yaml.safe_load(parts[1])
        assert frontmatter["name"] == "google-drive"
        assert frontmatter["connector"] == "gdrive_connector"
        assert "read" in frontmatter["capabilities"]

    def test_generate_skill_with_template_and_mcp_tools(
        self, skill_generator: SkillGenerator, mock_service_registry
    ):
        """Test generating skill with template and MCP tools."""
        mcp_tools = [
            {
                "name": "search_files",
                "description": "Search for files in Google Drive",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "max_results": {"type": "number", "description": "Max results"},
                    },
                    "required": ["query"],
                },
            },
        ]

        skill_md = skill_generator.generate_skill_md(
            service_name="google-drive",
            mount_path="/mnt/gdrive/",
            mcp_tools=mcp_tools,
        )

        assert "## MCP Tools" in skill_md
        assert "### search_files" in skill_md
        assert "Search for files in Google Drive" in skill_md
        assert "`query` (string) *(required)*" in skill_md
        assert "`max_results` (number)" in skill_md

    def test_generate_skill_with_tool_defs(
        self, skill_generator: SkillGenerator, mock_service_registry
    ):
        """Test generating skill with MCPToolDefinition objects."""
        tool_config = MCPToolConfig(
            endpoint="nexus://tools/test_tool",
            input_schema={
                "type": "object",
                "properties": {
                    "param1": {"type": "string", "description": "First parameter"},
                },
                "required": ["param1"],
            },
        )

        tool_def = MCPToolDefinition(
            name="test_tool",
            description="Test tool description",
            mcp_config=tool_config,
        )

        skill_md = skill_generator.generate_skill_md(
            service_name="google-drive",
            mount_path="/mnt/gdrive/",
            tool_defs=[tool_def],
        )

        assert "## MCP Tools" in skill_md
        assert "### test_tool" in skill_md
        assert "Test tool description" in skill_md
        assert "`param1` (string) *(required)*" in skill_md

    def test_generate_mcp_only_skill(self, skill_generator: SkillGenerator):
        """Test generating MCP-only skill without connector template."""
        mcp_mount = MCPMount(
            name="test-mcp",
            tier="tier1",
            transport="stdio",
            command="test-mcp-server",
            description="Test MCP server",
        )

        mcp_tools = [
            {
                "name": "tool1",
                "description": "Tool 1",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]

        skill_md = skill_generator.generate_skill_md(
            service_name="test-service",
            mount_path="/mnt/test/",
            mcp_tools=mcp_tools,
            mcp_mount=mcp_mount,
        )

        assert "---" in skill_md
        assert "test-service" in skill_md
        assert "/mnt/test/" in skill_md
        assert "## MCP Tools" in skill_md
        assert "### tool1" in skill_md

        # Parse frontmatter
        parts = skill_md.split("---")
        frontmatter = yaml.safe_load(parts[1])
        assert frontmatter["skill_type"] == "mcp_tools"
        assert frontmatter["tool_count"] == 1
        assert frontmatter["transport"] == "stdio"
        assert frontmatter["command"] == "test-mcp-server"

    def test_generate_basic_skill(self, skill_generator: SkillGenerator, mock_service_registry):
        """Test generating basic skill without template or MCP tools."""
        skill_md = skill_generator.generate_skill_md(
            service_name="gcs",
            mount_path="/mnt/gcs/",
        )

        assert "---" in skill_md
        assert "gcs" in skill_md
        assert "/mnt/gcs/" in skill_md

        # Parse frontmatter
        parts = skill_md.split("---")
        frontmatter = yaml.safe_load(parts[1])
        assert frontmatter["name"] == "gcs"
        assert frontmatter["skill_type"] == "service"
        assert "read" in frontmatter["capabilities"]

    def test_generate_skill_unknown_service(self, skill_generator: SkillGenerator):
        """Test generating skill for unknown service."""
        skill_md = skill_generator.generate_skill_md(
            service_name="unknown-service",
            mount_path="/mnt/unknown/",
        )

        # Should still generate a basic skill
        assert "---" in skill_md
        assert "unknown-service" in skill_md
        assert "/mnt/unknown/" in skill_md

        # Parse frontmatter
        parts = skill_md.split("---")
        frontmatter = yaml.safe_load(parts[1])
        assert frontmatter["name"] == "unknown-service"


class TestMetadataBuilding:
    """Tests for metadata building."""

    def test_build_metadata_with_service_info(
        self, skill_generator: SkillGenerator, mock_service_registry
    ):
        """Test building metadata from service info."""
        service_info = SERVICE_REGISTRY["google-drive"]

        metadata = skill_generator._build_metadata(
            service_name="google-drive",
            service_info=service_info,
            mcp_mount=None,
            tool_defs=None,
        )

        assert metadata.name == "google-drive"
        assert metadata.display_name == "Google Drive"
        assert metadata.connector == "gdrive_connector"
        assert metadata.oauth_provider == "google"
        assert "read" in metadata.capabilities

    def test_build_metadata_without_service_info(self, skill_generator: SkillGenerator):
        """Test building metadata without service info."""
        metadata = skill_generator._build_metadata(
            service_name="custom-service",
            service_info=None,
            mcp_mount=None,
            tool_defs=None,
        )

        assert metadata.name == "custom-service"
        assert metadata.display_name == "Custom Service"
        assert metadata.connector is None
        assert metadata.capabilities == []

    def test_build_metadata_with_mcp_mount(self, skill_generator: SkillGenerator):
        """Test building metadata with MCP mount."""
        mcp_mount = MCPMount(
            name="test-mcp",
            tier="tier1",
            transport="stdio",
            command="test-mcp-server",
            description="Custom MCP description",
        )

        metadata = skill_generator._build_metadata(
            service_name="test-service",
            service_info=None,
            mcp_mount=mcp_mount,
            tool_defs=None,
        )

        assert metadata.description == "Custom MCP description"
        assert "tools" in metadata.capabilities
        assert metadata.transport == "stdio"

    def test_build_metadata_with_tool_defs(self, skill_generator: SkillGenerator):
        """Test building metadata with tool definitions."""
        tool_config = MCPToolConfig(
            endpoint="nexus://tools/test",
            input_schema={},
        )
        tool_def = MCPToolDefinition(
            name="test_tool",
            description="Test",
            mcp_config=tool_config,
        )

        metadata = skill_generator._build_metadata(
            service_name="test-service",
            service_info=None,
            mcp_mount=None,
            tool_defs=[tool_def],
        )

        assert metadata.tool_count == 1


class TestToolsSectionGeneration:
    """Tests for MCP tools section generation."""

    def test_generate_tools_section_from_tool_defs(self, skill_generator: SkillGenerator):
        """Test generating tools section from MCPToolDefinition objects."""
        tool_config1 = MCPToolConfig(
            endpoint="nexus://tools/tool1",
            input_schema={
                "type": "object",
                "properties": {
                    "param1": {"type": "string", "description": "Parameter 1"},
                    "param2": {"type": "number", "description": "Parameter 2"},
                },
                "required": ["param1"],
            },
        )

        tool_config2 = MCPToolConfig(
            endpoint="nexus://tools/tool2",
            input_schema={"type": "object", "properties": {}},
        )

        tool_def1 = MCPToolDefinition(
            name="tool1",
            description="First tool",
            mcp_config=tool_config1,
        )

        tool_def2 = MCPToolDefinition(
            name="tool2",
            description="Second tool",
            mcp_config=tool_config2,
        )

        section = skill_generator._generate_tools_section([tool_def1, tool_def2])

        assert "## MCP Tools" in section
        assert "### tool1" in section
        assert "### tool2" in section
        assert "First tool" in section
        assert "`param1` (string) *(required)*: Parameter 1" in section
        assert "`param2` (number): Parameter 2" in section

    def test_generate_tools_section_from_dicts(self, skill_generator: SkillGenerator):
        """Test generating tools section from dictionaries."""
        tools = [
            {
                "name": "search",
                "description": "Search files",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "limit": {"type": "integer", "description": "Result limit"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "upload",
                "description": "Upload file",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path"},
                    },
                    "required": ["path"],
                },
            },
        ]

        section = skill_generator._generate_tools_section_from_dicts(tools)

        assert "## MCP Tools" in section
        assert "### search" in section
        assert "### upload" in section
        assert "Search files" in section
        assert "`query` (string) *(required)*: Search query" in section
        assert "`limit` (integer): Result limit" in section
        assert "`path` (string) *(required)*: File path" in section

    def test_generate_tools_section_sorted(self, skill_generator: SkillGenerator):
        """Test tools section is sorted alphabetically."""
        tools = [
            {"name": "zebra", "description": "Z tool", "inputSchema": {}},
            {"name": "apple", "description": "A tool", "inputSchema": {}},
            {"name": "middle", "description": "M tool", "inputSchema": {}},
        ]

        section = skill_generator._generate_tools_section_from_dicts(tools)

        # Check order by finding indices
        apple_idx = section.index("### apple")
        middle_idx = section.index("### middle")
        zebra_idx = section.index("### zebra")

        assert apple_idx < middle_idx < zebra_idx

    def test_generate_tools_section_empty_schema(self, skill_generator: SkillGenerator):
        """Test generating tools section with empty input schema."""
        tools = [
            {
                "name": "no_params",
                "description": "Tool without parameters",
                "inputSchema": {},
            },
        ]

        section = skill_generator._generate_tools_section_from_dicts(tools)

        assert "## MCP Tools" in section
        assert "### no_params" in section
        assert "Tool without parameters" in section
        assert "**Parameters:**" not in section


class TestPlaceholderReplacement:
    """Tests for {mount_path} placeholder replacement."""

    def test_mount_path_replacement_in_content(
        self, skill_generator: SkillGenerator, mock_service_registry
    ):
        """Test mount path placeholder is replaced in content."""
        skill_md = skill_generator.generate_skill_md(
            service_name="google-drive",
            mount_path="/mnt/custom/path",
        )

        assert "/mnt/custom/path" in skill_md
        assert "{mount_path}" not in skill_md

    def test_mount_path_replacement_in_description(
        self, skill_generator: SkillGenerator, mock_service_registry
    ):
        """Test mount path placeholder is replaced in description."""
        skill_md = skill_generator.generate_skill_md(
            service_name="google-drive",
            mount_path="/mnt/test/",
        )

        # Parse frontmatter to check description
        parts = skill_md.split("---")
        frontmatter = yaml.safe_load(parts[1])

        assert "/mnt/test/" in frontmatter["description"]
        assert "{mount_path}" not in frontmatter["description"]


class TestModuleLevelFunctions:
    """Tests for module-level convenience functions."""

    def test_get_skill_generator_singleton(self):
        """Test get_skill_generator returns singleton."""
        gen1 = get_skill_generator()
        gen2 = get_skill_generator()

        assert gen1 is gen2

    def test_generate_skill_md_function(self, mock_service_registry):
        """Test module-level generate_skill_md function."""
        skill_md = generate_skill_md(
            service_name="google-drive",
            mount_path="/mnt/gdrive/",
        )

        assert skill_md is not None
        assert "google-drive" in skill_md
        assert "/mnt/gdrive/" in skill_md

    def test_generate_skill_md_with_all_params(self, mock_service_registry):
        """Test module-level function with all parameters."""
        mcp_mount = MCPMount(
            name="test-mcp",
            description="Test MCP server",
            tier="tier1",
            transport="stdio",
            command="test",
        )

        mcp_tools = [
            {
                "name": "test_tool",
                "description": "Test",
                "inputSchema": {},
            },
        ]

        skill_md = generate_skill_md(
            service_name="google-drive",
            mount_path="/mnt/gdrive/",
            mcp_tools=mcp_tools,
            mcp_mount=mcp_mount,
        )

        assert "## MCP Tools" in skill_md
        assert "### test_tool" in skill_md


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_generate_with_special_characters_in_mount_path(
        self, skill_generator: SkillGenerator, mock_service_registry
    ):
        """Test generating skill with special characters in mount path."""
        skill_md = skill_generator.generate_skill_md(
            service_name="google-drive",
            mount_path="/mnt/gdrive/user+special@example.com",
        )

        assert "/mnt/gdrive/user+special@example.com" in skill_md

    def test_generate_with_empty_capabilities(self, skill_generator: SkillGenerator):
        """Test generating skill with empty capabilities list."""
        skill_md = skill_generator.generate_skill_md(
            service_name="test-service",
            mount_path="/mnt/test/",
        )

        # Should still generate valid YAML
        parts = skill_md.split("---")
        frontmatter = yaml.safe_load(parts[1])
        assert "name" in frontmatter

    def test_generate_with_none_mcp_tools(
        self, skill_generator: SkillGenerator, mock_service_registry
    ):
        """Test generating skill with None mcp_tools."""
        skill_md = skill_generator.generate_skill_md(
            service_name="google-drive",
            mount_path="/mnt/gdrive/",
            mcp_tools=None,
        )

        assert "## MCP Tools" not in skill_md

    def test_generate_with_empty_tool_list(
        self, skill_generator: SkillGenerator, mock_service_registry
    ):
        """Test generating skill with empty tool list."""
        skill_md = skill_generator.generate_skill_md(
            service_name="google-drive",
            mount_path="/mnt/gdrive/",
            mcp_tools=[],
        )

        # Empty list should not add tools section
        assert "## MCP Tools" not in skill_md


class TestConnectorTemplate:
    """Tests for ConnectorTemplate dataclass."""

    def test_connector_template_creation(self):
        """Test creating ConnectorTemplate instance."""
        template = ConnectorTemplate(
            name="test-service",
            service="test-service",
            connector="test_connector",
            oauth_provider="test",
            capabilities=["read", "write"],
            content="Template content",
            raw_content="---\nname: test\n---\nTemplate content",
        )

        assert template.name == "test-service"
        assert template.connector == "test_connector"
        assert template.oauth_provider == "test"
        assert "read" in template.capabilities


class TestSkillMetadata:
    """Tests for SkillMetadata dataclass."""

    def test_skill_metadata_defaults(self):
        """Test SkillMetadata with default values."""
        metadata = SkillMetadata(
            name="test",
            display_name="Test Service",
            description="Test description",
        )

        assert metadata.version == "1.0.0"
        assert metadata.skill_type == "service"
        assert metadata.capabilities == []
        assert metadata.connector is None
        assert metadata.tool_count == 0

    def test_skill_metadata_custom_values(self):
        """Test SkillMetadata with custom values."""
        metadata = SkillMetadata(
            name="test",
            display_name="Test",
            description="Test",
            version="2.0.0",
            skill_type="mcp_tools",
            capabilities=["tools"],
            connector="test_connector",
            mcp_name="test_mcp",
            oauth_provider="test_oauth",
            transport="stdio",
            tool_count=5,
        )

        assert metadata.version == "2.0.0"
        assert metadata.skill_type == "mcp_tools"
        assert metadata.tool_count == 5
        assert metadata.transport == "stdio"
