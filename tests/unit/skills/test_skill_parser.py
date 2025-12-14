"""Unit tests for skill parser."""

import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from nexus.skills.models import Skill, SkillMetadata
from nexus.skills.parser import SkillParseError, SkillParser

# Sample SKILL.md content for testing
SIMPLE_SKILL_MD = """---
name: test-skill
description: A simple test skill
---

# Test Skill

This is the skill content.
"""

FULL_SKILL_MD = """---
name: advanced-skill
description: An advanced test skill
version: 1.0.0
author: Test Author
requires:
  - dependency-1
  - dependency-2
created_at: 2025-01-01T00:00:00
modified_at: 2025-01-02T00:00:00
custom_field: custom_value
---

# Advanced Skill

This is the advanced skill content with:
- Multiple sections
- Code examples
- And more!

## Section 1

Content here...
"""

INVALID_NO_FRONTMATTER = """# Skill Without Frontmatter

This skill is missing the YAML frontmatter.
"""

INVALID_YAML = """---
name: test-skill
description: broken yaml
  invalid: indentation
---

Content
"""

MISSING_NAME = """---
description: Skill without name
---

Content
"""

MISSING_DESCRIPTION = """---
name: test-skill
---

Content
"""


def test_parse_simple_skill() -> None:
    """Test parsing a simple SKILL.md file."""
    parser = SkillParser()
    skill = parser.parse_content(SIMPLE_SKILL_MD)

    assert isinstance(skill, Skill)
    assert skill.metadata.name == "test-skill"
    assert skill.metadata.description == "A simple test skill"
    assert "# Test Skill" in skill.content
    assert "This is the skill content." in skill.content


def test_parse_full_skill() -> None:
    """Test parsing a complete SKILL.md file with all fields."""
    parser = SkillParser()
    skill = parser.parse_content(FULL_SKILL_MD)

    assert skill.metadata.name == "advanced-skill"
    assert skill.metadata.description == "An advanced test skill"
    assert skill.metadata.version == "1.0.0"
    assert skill.metadata.author == "Test Author"
    assert skill.metadata.requires == ["dependency-1", "dependency-2"]
    assert skill.metadata.created_at == datetime(2025, 1, 1, 0, 0, 0)
    assert skill.metadata.modified_at == datetime(2025, 1, 2, 0, 0, 0)
    assert skill.metadata.metadata["custom_field"] == "custom_value"
    assert "# Advanced Skill" in skill.content


def test_parse_file() -> None:
    """Test parsing from an actual file."""
    parser = SkillParser()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(SIMPLE_SKILL_MD)
        temp_path = f.name

    try:
        skill = parser.parse_file(temp_path)
        assert skill.metadata.name == "test-skill"
        assert skill.metadata.file_path == temp_path
    finally:
        Path(temp_path).unlink()


def test_parse_file_with_tier() -> None:
    """Test parsing file with tier specified."""
    parser = SkillParser()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(SIMPLE_SKILL_MD)
        temp_path = f.name

    try:
        skill = parser.parse_file(temp_path, tier="user")
        assert skill.metadata.tier == "user"
    finally:
        Path(temp_path).unlink()


def test_parse_file_not_found() -> None:
    """Test that parsing non-existent file raises error."""
    parser = SkillParser()

    with pytest.raises(SkillParseError, match="Skill file not found"):
        parser.parse_file("/nonexistent/path/SKILL.md")


def test_parse_no_frontmatter() -> None:
    """Test that parsing without frontmatter raises error."""
    parser = SkillParser()

    with pytest.raises(SkillParseError, match="must have YAML frontmatter"):
        parser.parse_content(INVALID_NO_FRONTMATTER)


def test_parse_invalid_yaml() -> None:
    """Test that parsing invalid YAML raises error."""
    parser = SkillParser()

    with pytest.raises(SkillParseError, match="Invalid YAML frontmatter"):
        parser.parse_content(INVALID_YAML)


def test_parse_missing_name() -> None:
    """Test that parsing without name raises error."""
    parser = SkillParser()

    with pytest.raises(SkillParseError, match="'name' is required"):
        parser.parse_content(MISSING_NAME)


def test_parse_missing_description() -> None:
    """Test that parsing without description raises error."""
    parser = SkillParser()

    with pytest.raises(SkillParseError, match="'description' is required"):
        parser.parse_content(MISSING_DESCRIPTION)


def test_parse_metadata_only() -> None:
    """Test parsing metadata only (for progressive disclosure)."""
    parser = SkillParser()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(FULL_SKILL_MD)
        temp_path = f.name

    try:
        metadata = parser.parse_metadata_only(temp_path)

        assert isinstance(metadata, SkillMetadata)
        assert metadata.name == "advanced-skill"
        assert metadata.description == "An advanced test skill"
        assert metadata.version == "1.0.0"
        assert metadata.author == "Test Author"
        assert metadata.requires == ["dependency-1", "dependency-2"]
        assert metadata.file_path == temp_path
    finally:
        Path(temp_path).unlink()


def test_parse_metadata_only_with_tier() -> None:
    """Test parsing metadata with tier specified."""
    parser = SkillParser()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(SIMPLE_SKILL_MD)
        temp_path = f.name

    try:
        metadata = parser.parse_metadata_only(temp_path, tier="tenant")
        assert metadata.tier == "tenant"
    finally:
        Path(temp_path).unlink()


def test_parse_requires_single_string() -> None:
    """Test that 'requires' can be a single string."""
    skill_md = """---
name: test-skill
description: Test
requires: single-dependency
---

Content
"""
    parser = SkillParser()
    skill = parser.parse_content(skill_md)

    assert skill.metadata.requires == ["single-dependency"]


def test_parse_requires_invalid_type() -> None:
    """Test that invalid 'requires' type raises error."""
    skill_md = """---
name: test-skill
description: Test
requires: 123
---

Content
"""
    parser = SkillParser()

    with pytest.raises(SkillParseError, match="'requires' must be a list or string"):
        parser.parse_content(skill_md)


def test_parse_datetime_formats() -> None:
    """Test parsing various datetime formats."""
    skill_md = """---
name: test-skill
description: Test
created_at: 2025-01-01T00:00:00
modified_at: 2025-01-01T12:30:45.123456
---

Content
"""
    parser = SkillParser()
    skill = parser.parse_content(skill_md)

    assert skill.metadata.created_at == datetime(2025, 1, 1, 0, 0, 0)
    assert skill.metadata.modified_at == datetime(2025, 1, 1, 12, 30, 45, 123456)


def test_parse_invalid_datetime() -> None:
    """Test that invalid datetime is ignored with warning."""
    skill_md = """---
name: test-skill
description: Test
created_at: not-a-datetime
---

Content
"""
    parser = SkillParser()
    skill = parser.parse_content(skill_md)

    # Should not raise, but datetime should be None
    assert skill.metadata.created_at is None


def test_parse_frontmatter_not_dict() -> None:
    """Test that non-dict frontmatter raises error."""
    skill_md = """---
- item1
- item2
---

Content
"""
    parser = SkillParser()

    with pytest.raises(SkillParseError, match="Frontmatter must be a YAML dict"):
        parser.parse_content(skill_md)


def test_parse_additional_metadata() -> None:
    """Test that additional metadata fields are preserved."""
    skill_md = """---
name: test-skill
description: Test
custom_field: custom_value
another_field: 123
---

Content
"""
    parser = SkillParser()
    skill = parser.parse_content(skill_md)

    assert skill.metadata.metadata["custom_field"] == "custom_value"
    assert skill.metadata.metadata["another_field"] == 123


def test_parse_empty_content() -> None:
    """Test parsing skill with empty content after frontmatter."""
    skill_md = """---
name: test-skill
description: Test
---
"""
    parser = SkillParser()
    skill = parser.parse_content(skill_md)

    # Empty content should be stripped to empty string
    assert skill.content == ""


def test_parse_whitespace_handling() -> None:
    """Test that content whitespace is properly handled."""
    skill_md = """---
name: test-skill
description: Test
---

# Content

Some text here.
"""
    parser = SkillParser()
    skill = parser.parse_content(skill_md)

    # Content should be stripped but preserve internal structure
    assert skill.content.startswith("# Content")
    assert skill.content.endswith("Some text here.")
