"""Unit tests for skill models."""

from datetime import datetime

import pytest

from nexus.core.exceptions import ValidationError
from nexus.skills.models import Skill, SkillExportManifest, SkillMetadata


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

    with pytest.raises(ValidationError, match="skill tier must be 'agent', 'tenant', or 'system'"):
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


def test_skill_export_manifest_initialization() -> None:
    """Test SkillExportManifest initialization."""
    manifest = SkillExportManifest(name="test-skill")

    assert manifest.name == "test-skill"
    assert manifest.version is None
    assert manifest.description == ""
    assert manifest.format == "generic"
    assert manifest.exported_at is None
    assert manifest.files == []
    assert manifest.total_size_bytes == 0


def test_skill_export_manifest_with_all_fields() -> None:
    """Test SkillExportManifest with all fields."""
    now = datetime.utcnow()
    manifest = SkillExportManifest(
        name="test-skill",
        version="1.0.0",
        description="Test skill export",
        format="claude",
        exported_at=now,
        files=["test-skill/SKILL.md"],
        total_size_bytes=1024,
    )

    assert manifest.name == "test-skill"
    assert manifest.version == "1.0.0"
    assert manifest.description == "Test skill export"
    assert manifest.format == "claude"
    assert manifest.exported_at == now
    assert manifest.files == ["test-skill/SKILL.md"]
    assert manifest.total_size_bytes == 1024


def test_skill_export_manifest_validation() -> None:
    """Test SkillExportManifest validation."""
    manifest = SkillExportManifest(
        name="test-skill",
        files=["test-skill/SKILL.md"],
    )

    manifest.validate()  # Should not raise


def test_skill_export_manifest_validation_missing_name() -> None:
    """Test that validation fails when name is missing."""
    manifest = SkillExportManifest(name="", files=["test.md"])

    with pytest.raises(ValidationError, match="export manifest name is required"):
        manifest.validate()


def test_skill_export_manifest_validation_missing_files() -> None:
    """Test that validation fails when files list is empty."""
    manifest = SkillExportManifest(name="test-skill", files=[])

    with pytest.raises(ValidationError, match="export manifest must include at least one file"):
        manifest.validate()


def test_skill_export_manifest_validation_claude_size_limit() -> None:
    """Test that validation fails when Claude format exceeds 8MB limit."""
    manifest = SkillExportManifest(
        name="test-skill",
        format="claude",
        files=["test.md"],
        total_size_bytes=9 * 1024 * 1024,  # 9MB
    )

    with pytest.raises(ValidationError, match="exceeds Claude format 8MB limit"):
        manifest.validate()


def test_skill_export_manifest_validation_claude_size_under_limit() -> None:
    """Test that validation passes when Claude format is under 8MB."""
    manifest = SkillExportManifest(
        name="test-skill",
        format="claude",
        files=["test.md"],
        total_size_bytes=7 * 1024 * 1024,  # 7MB
    )

    manifest.validate()  # Should not raise


def test_skill_export_manifest_validation_generic_no_limit() -> None:
    """Test that generic format has no size limit."""
    manifest = SkillExportManifest(
        name="test-skill",
        format="generic",
        files=["test.md"],
        total_size_bytes=100 * 1024 * 1024,  # 100MB
    )

    manifest.validate()  # Should not raise
