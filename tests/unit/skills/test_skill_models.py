"""Unit tests for skill models."""

from datetime import datetime

import pytest

from nexus.core.exceptions import ValidationError
from nexus.skills.models import Skill, SkillMetadata


def test_skill_metadata_initialization() -> None:
    """Test SkillMetadata initialization with required fields."""
    metadata = SkillMetadata(
        name="test-skill",
        description="A test skill",
    )

    assert metadata.name == "test-skill"
    assert metadata.description == "A test skill"
    assert metadata.version is None
    assert metadata.author is None
    assert metadata.requires == []
    assert metadata.metadata == {}
    assert metadata.file_path is None
    assert metadata.tier is None


def test_skill_metadata_with_all_fields() -> None:
    """Test SkillMetadata with all optional fields."""
    now = datetime.utcnow()
    metadata = SkillMetadata(
        name="advanced-skill",
        description="An advanced skill",
        version="1.0.0",
        author="Test Author",
        created_at=now,
        modified_at=now,
        requires=["dependency-1", "dependency-2"],
        metadata={"custom_field": "value"},
        file_path="/path/to/SKILL.md",
        tier="agent",
    )

    assert metadata.name == "advanced-skill"
    assert metadata.description == "An advanced skill"
    assert metadata.version == "1.0.0"
    assert metadata.author == "Test Author"
    assert metadata.created_at == now
    assert metadata.modified_at == now
    assert metadata.requires == ["dependency-1", "dependency-2"]
    assert metadata.metadata == {"custom_field": "value"}
    assert metadata.file_path == "/path/to/SKILL.md"
    assert metadata.tier == "agent"


def test_skill_metadata_validation_missing_name() -> None:
    """Test that validation fails when name is missing."""
    metadata = SkillMetadata(name="", description="Test")

    with pytest.raises(ValidationError, match="skill name is required"):
        metadata.validate()


def test_skill_metadata_validation_missing_description() -> None:
    """Test that validation fails when description is missing."""
    metadata = SkillMetadata(name="test-skill", description="")

    with pytest.raises(ValidationError, match="skill description is required"):
        metadata.validate()


def test_skill_metadata_validation_invalid_name() -> None:
    """Test that validation fails with invalid name characters."""
    metadata = SkillMetadata(
        name="invalid skill!",  # Space and ! are invalid
        description="Test",
    )

    with pytest.raises(ValidationError, match="skill name must be alphanumeric"):
        metadata.validate()


def test_skill_metadata_validation_valid_names() -> None:
    """Test that validation passes with valid name formats."""
    valid_names = ["skill", "my-skill", "skill_123", "my-skill-v2"]

    for name in valid_names:
        metadata = SkillMetadata(name=name, description="Test")
        metadata.validate()  # Should not raise


def test_skill_metadata_validation_invalid_tier() -> None:
    """Test that validation fails with invalid tier."""
    metadata = SkillMetadata(
        name="test-skill",
        description="Test",
        tier="invalid-tier",
    )

    with pytest.raises(ValidationError, match="skill tier must be one of"):
        metadata.validate()


def test_skill_metadata_validation_valid_tiers() -> None:
    """Test that validation passes with valid tiers."""
    for tier in ["agent", "tenant", "system"]:
        metadata = SkillMetadata(
            name="test-skill",
            description="Test",
            tier=tier,
        )
        metadata.validate()  # Should not raise


def test_skill_initialization() -> None:
    """Test Skill initialization."""
    metadata = SkillMetadata(name="test-skill", description="Test")
    skill = Skill(metadata=metadata, content="# Skill Content\n\nSome markdown.")

    assert skill.metadata == metadata
    assert skill.content == "# Skill Content\n\nSome markdown."


def test_skill_validation() -> None:
    """Test Skill validation."""
    metadata = SkillMetadata(name="test-skill", description="Test")
    skill = Skill(metadata=metadata, content="# Skill Content")

    skill.validate()  # Should not raise


def test_skill_validation_missing_content() -> None:
    """Test that validation fails when content is missing."""
    metadata = SkillMetadata(name="test-skill", description="Test")
    skill = Skill(metadata=metadata, content="")

    with pytest.raises(ValidationError, match="skill content is required"):
        skill.validate()


def test_skill_validation_invalid_metadata() -> None:
    """Test that validation fails when metadata is invalid."""
    metadata = SkillMetadata(name="", description="Test")
    skill = Skill(metadata=metadata, content="# Content")

    with pytest.raises(ValidationError, match="skill name is required"):
        skill.validate()
