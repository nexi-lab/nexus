"""Unit tests for skill templates."""

import pytest

from nexus.skills.templates import (
    TemplateError,
    get_template,
    get_template_description,
    list_templates,
)


def test_list_templates() -> None:
    """Test listing available templates."""
    templates = list_templates()

    assert isinstance(templates, list)
    assert len(templates) > 0
    assert "basic" in templates
    assert "data-analysis" in templates
    assert "code-generation" in templates
    assert "document-processing" in templates
    assert "api-integration" in templates


def test_get_template_basic() -> None:
    """Test getting basic template."""
    content = get_template("basic", name="my-skill", description="My test skill")

    assert isinstance(content, str)
    assert "my-skill" in content
    assert "My test skill" in content
    assert "# my-skill" in content
    assert "Overview" in content
    assert "Usage" in content


def test_get_template_data_analysis() -> None:
    """Test getting data-analysis template."""
    content = get_template("data-analysis", name="data-analyzer", description="Analyzes data")

    assert "data-analyzer" in content
    assert "Analyzes data" in content
    assert "Data Loading" in content
    assert "Statistical Analysis" in content
    assert "Visualization" in content


def test_get_template_code_generation() -> None:
    """Test getting code-generation template."""
    content = get_template("code-generation", name="code-gen", description="Generates code")

    assert "code-gen" in content
    assert "Generates code" in content
    assert "Code Generation" in content
    assert "Refactoring" in content
    assert "Best Practices" in content


def test_get_template_document_processing() -> None:
    """Test getting document-processing template."""
    content = get_template(
        "document-processing", name="doc-processor", description="Processes documents"
    )

    assert "doc-processor" in content
    assert "Processes documents" in content
    assert "Document Parsing" in content
    assert "Text Extraction" in content
    assert "Supported Formats" in content


def test_get_template_api_integration() -> None:
    """Test getting api-integration template."""
    content = get_template("api-integration", name="api-client", description="API integration")

    assert "api-client" in content
    assert "API integration" in content
    assert "HTTP Requests" in content
    assert "Authentication" in content
    assert "Rate Limiting" in content


def test_get_template_all_templates() -> None:
    """Test that all templates can be loaded."""
    templates = list_templates()

    for template_name in templates:
        content = get_template(template_name, name="test-skill", description="Test description")

        assert isinstance(content, str)
        assert len(content) > 0
        assert "test-skill" in content
        assert "Test description" in content


def test_get_template_not_found() -> None:
    """Test that getting non-existent template raises error."""
    with pytest.raises(TemplateError, match="Template 'nonexistent' not found"):
        get_template("nonexistent", name="test", description="test")


def test_get_template_missing_variable() -> None:
    """Test that missing required variables raise error."""
    with pytest.raises(TemplateError, match="Missing required variable"):
        get_template("basic", name="test")  # Missing description


def test_get_template_extra_variables() -> None:
    """Test that extra variables are ignored."""
    # Should not raise error
    content = get_template(
        "basic",
        name="test",
        description="test",
        extra_var="ignored",
        another_var="also ignored",
    )

    assert "test" in content


def test_get_template_description() -> None:
    """Test getting template descriptions."""
    desc = get_template_description("basic")
    assert isinstance(desc, str)
    assert len(desc) > 0
    assert "simple" in desc.lower() or "general" in desc.lower()

    desc = get_template_description("data-analysis")
    assert "data" in desc.lower()
    assert "analysis" in desc.lower()


def test_get_template_description_all() -> None:
    """Test that all templates have descriptions."""
    templates = list_templates()

    for template_name in templates:
        desc = get_template_description(template_name)
        assert isinstance(desc, str)
        assert len(desc) > 0


def test_get_template_description_not_found() -> None:
    """Test that getting description for non-existent template raises error."""
    with pytest.raises(TemplateError, match="Template 'nonexistent' not found"):
        get_template_description("nonexistent")


def test_template_format_consistency() -> None:
    """Test that all templates have consistent format."""
    templates = list_templates()

    for template_name in templates:
        content = get_template(template_name, name="test-skill", description="Test description")

        # All templates should start with a header
        assert content.startswith("# test-skill")

        # All templates should include the description early on
        lines = content.split("\n")
        assert "Test description" in "\n".join(lines[:10])


def test_template_content_quality() -> None:
    """Test that templates contain useful content."""
    templates = list_templates()

    for template_name in templates:
        content = get_template(template_name, name="test-skill", description="Test description")

        # Should be substantial content
        # Basic template is intentionally simple (>200 chars), others are more comprehensive (>500 chars)
        min_length = 200 if template_name == "basic" else 500
        assert len(content) > min_length, (
            f"Template {template_name} is too short: {len(content)} chars"
        )

        # Should have multiple sections (at least 2 ## headers)
        section_count = content.count("\n## ")
        assert section_count >= 2, f"Template {template_name} has only {section_count} sections"


def test_basic_template_structure() -> None:
    """Test basic template has expected structure."""
    content = get_template("basic", name="test", description="Test skill")

    # Check for expected sections
    assert "## Overview" in content
    assert "## Usage" in content
    assert "## Example" in content
    assert "## Notes" in content


def test_data_analysis_template_structure() -> None:
    """Test data-analysis template has expected structure."""
    content = get_template("data-analysis", name="test", description="Test skill")

    # Check for expected sections
    assert "## Overview" in content
    assert "## Capabilities" in content
    assert "## Workflow" in content
    assert "## Example" in content
    assert "## Dependencies" in content
    assert "## Notes" in content

    # Check for key capabilities
    assert "Data Loading" in content
    assert "Data Cleaning" in content
    assert "Statistical Analysis" in content
    assert "Visualization" in content


def test_code_generation_template_structure() -> None:
    """Test code-generation template has expected structure."""
    content = get_template("code-generation", name="test", description="Test skill")

    # Check for expected sections
    assert "## Overview" in content
    assert "## Capabilities" in content
    assert "## Workflow" in content
    assert "## Best Practices" in content
    assert "## Supported Languages" in content

    # Check for key capabilities
    assert "Code Generation" in content
    assert "Refactoring" in content
    assert "Testing" in content


def test_document_processing_template_structure() -> None:
    """Test document-processing template has expected structure."""
    content = get_template("document-processing", name="test", description="Test skill")

    # Check for expected sections
    assert "## Overview" in content
    assert "## Capabilities" in content
    assert "## Workflow" in content
    assert "## Supported Formats" in content
    assert "## Dependencies" in content

    # Check for key capabilities
    assert "Document Parsing" in content
    assert "Text Extraction" in content
    assert "Entity Recognition" in content


def test_api_integration_template_structure() -> None:
    """Test api-integration template has expected structure."""
    content = get_template("api-integration", name="test", description="Test skill")

    # Check for expected sections
    assert "## Overview" in content
    assert "## Capabilities" in content
    assert "## Workflow" in content
    assert "## Best Practices" in content
    assert "## Common Patterns" in content

    # Check for key capabilities
    assert "HTTP Requests" in content
    assert "Authentication" in content
    assert "Rate Limiting" in content
    assert "Error Handling" in content
