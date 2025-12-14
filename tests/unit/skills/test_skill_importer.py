"""Tests for skill importer module."""

import io
import zipfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.core.exceptions import PermissionDeniedError
from nexus.core.permissions import OperationContext
from nexus.skills.importer import SkillImporter, SkillImportError

# Mock SKILL.md content with valid YAML frontmatter
VALID_SKILL_MD = """---
name: test-skill
description: A test skill for unit testing
version: 1.0.0
author: Test Author
requires:
  - dependency-skill
tags:
  - test
  - example
skill_type: documentation
---

# Test Skill

This is a test skill used for unit testing the importer.

## Features

- Feature 1
- Feature 2

## Usage

Example usage instructions here.
"""

INVALID_YAML_SKILL_MD = """---
name: test-skill
description: Missing closing ---
version: 1.0.0

# Test Skill

This will fail to parse.
"""

MISSING_NAME_SKILL_MD = """---
description: Missing name field
version: 1.0.0
---

# Test Skill

This is missing the required name field.
"""

MISSING_DESCRIPTION_SKILL_MD = """---
name: test-skill
version: 1.0.0
---

# Test Skill

This is missing the required description field.
"""


def create_mock_skill_zip(
    skill_name: str, skill_md_content: str, include_extras: bool = True
) -> bytes:
    """Create a mock skill ZIP file for testing.

    Args:
        skill_name: Name of the skill directory
        skill_md_content: Content for SKILL.md file
        include_extras: Whether to include optional files

    Returns:
        ZIP file as bytes
    """
    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        # Add SKILL.md (required)
        zip_file.writestr(f"{skill_name}/SKILL.md", skill_md_content)

        if include_extras:
            # Add optional files
            zip_file.writestr(f"{skill_name}/LICENSE.txt", "MIT License\n\nCopyright (c) 2025")
            zip_file.writestr(
                f"{skill_name}/reference.md", "# Reference\n\nAdditional documentation."
            )
            zip_file.writestr(f"{skill_name}/scripts/helper.py", "# Helper script\nprint('hello')")
            zip_file.writestr(
                f"{skill_name}/assets/icon.png", b"\x89PNG\r\n\x1a\n"
            )  # Mock PNG header

    return zip_buffer.getvalue()


def create_invalid_structure_zip() -> bytes:
    """Create a ZIP with invalid structure (SKILL.md not at root of skill directory)."""
    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        # SKILL.md is nested too deep
        zip_file.writestr("test-skill/subdir/SKILL.md", VALID_SKILL_MD)

    return zip_buffer.getvalue()


def create_no_skill_md_zip() -> bytes:
    """Create a ZIP without SKILL.md file."""
    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        zip_file.writestr("test-skill/README.md", "# Test\n\nNo SKILL.md here.")

    return zip_buffer.getvalue()


@pytest.fixture
def mock_filesystem():
    """Create a mock filesystem."""
    mock_fs = MagicMock()
    mock_fs.stat = MagicMock(return_value=None)  # File doesn't exist (no conflict)
    mock_fs.exists = MagicMock(return_value=False)  # File doesn't exist (no conflict)
    mock_fs.mkdir = MagicMock()
    mock_fs.write = MagicMock()
    return mock_fs


@pytest.fixture
def mock_registry():
    """Create a mock skill registry."""
    mock_reg = MagicMock()
    mock_reg.discover = AsyncMock()
    return mock_reg


@pytest.fixture
def importer(mock_filesystem, mock_registry):
    """Create a SkillImporter instance with mocks."""
    return SkillImporter(mock_filesystem, mock_registry)


@pytest.fixture
def admin_context():
    """Create an admin operation context."""
    return OperationContext(
        user="admin",
        user_id="admin",
        agent_id=None,
        subject_type="user",
        subject_id="admin",
        tenant_id="default",
        groups=[],
        is_admin=True,
        is_system=False,
        admin_capabilities=set(),
        request_id="test-request-123",
    )


@pytest.fixture
def user_context():
    """Create a regular user operation context."""
    return OperationContext(
        user="alice",
        user_id="alice",
        agent_id=None,
        subject_type="user",
        subject_id="alice",
        tenant_id="default",
        groups=[],
        is_admin=False,
        is_system=False,
        admin_capabilities=set(),
        request_id="test-request-456",
    )


class TestSkillImporter:
    """Test suite for SkillImporter."""

    @pytest.mark.asyncio
    async def test_import_valid_skill_user_tier(self, importer, user_context):
        """Test importing a valid skill to user tier."""
        zip_data = create_mock_skill_zip("test-skill", VALID_SKILL_MD)

        result = await importer.import_from_zip(
            zip_data=zip_data,
            tier="user",
            allow_overwrite=False,
            context=user_context,
        )

        assert result["imported_skills"] == ["test-skill"]
        assert len(result["skill_paths"]) == 1
        assert result["tier"] == "user"
        assert "/skills/users/alice/test-skill/" in result["skill_paths"][0]

    @pytest.mark.asyncio
    async def test_import_valid_skill_system_tier_as_admin(self, importer, admin_context):
        """Test importing a valid skill to system tier as admin."""
        zip_data = create_mock_skill_zip("test-skill", VALID_SKILL_MD)

        result = await importer.import_from_zip(
            zip_data=zip_data,
            tier="system",
            allow_overwrite=False,
            context=admin_context,
        )

        assert result["imported_skills"] == ["test-skill"]
        assert result["tier"] == "system"
        assert "/skill/test-skill/" in result["skill_paths"][0]

    @pytest.mark.asyncio
    async def test_import_system_tier_as_user_fails(self, importer, user_context):
        """Test that importing to system tier as regular user fails."""
        zip_data = create_mock_skill_zip("test-skill", VALID_SKILL_MD)

        with pytest.raises(PermissionDeniedError, match="Only admins can import to system tier"):
            await importer.import_from_zip(
                zip_data=zip_data,
                tier="system",
                allow_overwrite=False,
                context=user_context,
            )

    @pytest.mark.asyncio
    async def test_import_invalid_zip(self, importer, user_context):
        """Test importing invalid ZIP data."""
        invalid_zip = b"This is not a valid ZIP file"

        with pytest.raises(SkillImportError, match="Invalid ZIP file"):
            await importer.import_from_zip(
                zip_data=invalid_zip,
                tier="user",
                allow_overwrite=False,
                context=user_context,
            )

    @pytest.mark.asyncio
    async def test_import_no_skill_md(self, importer, user_context):
        """Test importing ZIP without SKILL.md file."""
        zip_data = create_no_skill_md_zip()

        with pytest.raises(SkillImportError, match="No SKILL.md file found"):
            await importer.import_from_zip(
                zip_data=zip_data,
                tier="user",
                allow_overwrite=False,
                context=user_context,
            )

    @pytest.mark.asyncio
    async def test_import_invalid_structure(self, importer, user_context):
        """Test importing ZIP with invalid directory structure."""
        zip_data = create_invalid_structure_zip()

        with pytest.raises(SkillImportError, match="Invalid skill package"):
            await importer.import_from_zip(
                zip_data=zip_data,
                tier="user",
                allow_overwrite=False,
                context=user_context,
            )

    @pytest.mark.asyncio
    async def test_import_invalid_yaml(self, importer, user_context):
        """Test importing skill with invalid YAML frontmatter."""
        zip_data = create_mock_skill_zip("test-skill", INVALID_YAML_SKILL_MD, include_extras=False)

        with pytest.raises(SkillImportError, match="Invalid skill package"):
            await importer.import_from_zip(
                zip_data=zip_data,
                tier="user",
                allow_overwrite=False,
                context=user_context,
            )

    @pytest.mark.asyncio
    async def test_import_missing_required_fields(self, importer, user_context):
        """Test importing skill with missing required fields."""
        # Test missing name
        zip_data = create_mock_skill_zip("test-skill", MISSING_NAME_SKILL_MD, include_extras=False)
        with pytest.raises(SkillImportError, match="Invalid skill package"):
            await importer.import_from_zip(
                zip_data=zip_data,
                tier="user",
                allow_overwrite=False,
                context=user_context,
            )

        # Test missing description
        zip_data = create_mock_skill_zip(
            "test-skill", MISSING_DESCRIPTION_SKILL_MD, include_extras=False
        )
        with pytest.raises(SkillImportError, match="Invalid skill package"):
            await importer.import_from_zip(
                zip_data=zip_data,
                tier="user",
                allow_overwrite=False,
                context=user_context,
            )

    @pytest.mark.asyncio
    async def test_import_name_conflict(self, importer, user_context):
        """Test importing skill with name conflict."""
        zip_data = create_mock_skill_zip("test-skill", VALID_SKILL_MD)

        # Mock exists to return True (file exists)
        importer._filesystem.exists = MagicMock(return_value=True)

        with pytest.raises(SkillImportError, match="already exist"):
            await importer.import_from_zip(
                zip_data=zip_data,
                tier="user",
                allow_overwrite=False,
                context=user_context,
            )

    @pytest.mark.asyncio
    async def test_import_name_conflict_with_overwrite(self, importer, user_context):
        """Test importing skill with name conflict and overwrite enabled."""
        zip_data = create_mock_skill_zip("test-skill", VALID_SKILL_MD)

        # Mock exists to return True (file exists)
        importer._filesystem.exists = MagicMock(return_value=True)

        # Should succeed with overwrite=True
        result = await importer.import_from_zip(
            zip_data=zip_data,
            tier="user",
            allow_overwrite=True,
            context=user_context,
        )

        assert result["imported_skills"] == ["test-skill"]

    @pytest.mark.asyncio
    async def test_import_invalid_skill_name(self, importer, user_context):
        """Test importing skill with invalid name (special characters)."""
        # Create skill with invalid name
        invalid_skill_md = VALID_SKILL_MD.replace("name: test-skill", "name: test@skill!")
        zip_data = create_mock_skill_zip("test@skill!", invalid_skill_md, include_extras=False)

        with pytest.raises(SkillImportError, match="Invalid skill package"):
            await importer.import_from_zip(
                zip_data=zip_data,
                tier="user",
                allow_overwrite=False,
                context=user_context,
            )

    @pytest.mark.asyncio
    async def test_import_multiple_skills_in_zip(self, importer, user_context):
        """Test importing ZIP containing multiple skills."""
        zip_buffer = io.BytesIO()

        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            # Add two skills
            zip_file.writestr(
                "skill-one/SKILL.md", VALID_SKILL_MD.replace("test-skill", "skill-one")
            )
            skill_two_md = VALID_SKILL_MD.replace("test-skill", "skill-two").replace(
                "A test skill", "Another test skill"
            )
            zip_file.writestr("skill-two/SKILL.md", skill_two_md)

        zip_data = zip_buffer.getvalue()

        result = await importer.import_from_zip(
            zip_data=zip_data,
            tier="user",
            allow_overwrite=False,
            context=user_context,
        )

        assert len(result["imported_skills"]) == 2
        assert "skill-one" in result["imported_skills"]
        assert "skill-two" in result["imported_skills"]
        assert len(result["skill_paths"]) == 2


class TestSkillValidation:
    """Test suite for skill ZIP validation."""

    @pytest.mark.asyncio
    async def test_validate_valid_zip(self, importer):
        """Test validating a valid skill ZIP."""
        zip_data = create_mock_skill_zip("test-skill", VALID_SKILL_MD)

        result = await importer.validate_zip(zip_data)

        assert result["valid"] is True
        assert "test-skill" in result["skills_found"]
        assert len(result["errors"]) == 0

    @pytest.mark.asyncio
    async def test_validate_invalid_zip(self, importer):
        """Test validating invalid ZIP data."""
        invalid_zip = b"Not a ZIP file"

        result = await importer.validate_zip(invalid_zip)

        assert result["valid"] is False
        assert len(result["errors"]) > 0
        assert any("Invalid ZIP file" in error for error in result["errors"])

    @pytest.mark.asyncio
    async def test_validate_no_skill_md(self, importer):
        """Test validating ZIP without SKILL.md."""
        zip_data = create_no_skill_md_zip()

        result = await importer.validate_zip(zip_data)

        assert result["valid"] is False
        assert len(result["errors"]) > 0
        assert any("No SKILL.md file found" in error for error in result["errors"])

    @pytest.mark.asyncio
    async def test_validate_invalid_structure(self, importer):
        """Test validating ZIP with invalid structure."""
        zip_data = create_invalid_structure_zip()

        result = await importer.validate_zip(zip_data)

        assert result["valid"] is False
        assert len(result["errors"]) > 0

    @pytest.mark.asyncio
    async def test_validate_missing_version_warning(self, importer):
        """Test that missing version generates warning."""
        skill_md_no_version = VALID_SKILL_MD.replace("version: 1.0.0\n", "")
        zip_data = create_mock_skill_zip("test-skill", skill_md_no_version, include_extras=False)

        result = await importer.validate_zip(zip_data)

        # Should be valid but have warnings
        assert result["valid"] is True
        assert len(result["warnings"]) > 0
        assert any("version" in warning.lower() for warning in result["warnings"])

    @pytest.mark.asyncio
    async def test_validate_missing_author_warning(self, importer):
        """Test that missing author generates warning."""
        skill_md_no_author = VALID_SKILL_MD.replace("author: Test Author\n", "")
        zip_data = create_mock_skill_zip("test-skill", skill_md_no_author, include_extras=False)

        result = await importer.validate_zip(zip_data)

        # Should be valid but have warnings
        assert result["valid"] is True
        assert len(result["warnings"]) > 0
        assert any("author" in warning.lower() for warning in result["warnings"])


class TestSkillNameValidation:
    """Test suite for skill name validation."""

    def test_valid_skill_names(self, importer):
        """Test that valid skill names pass validation."""
        valid_names = [
            "test-skill",
            "my_skill",
            "skill123",
            "test-skill-2",
            "my_awesome_skill",
            "a",  # Single character
            "skill-with-many-hyphens",
            "skill_with_many_underscores",
        ]

        for name in valid_names:
            # Should match the pattern
            assert importer.VALID_NAME_PATTERN.match(name), f"Expected '{name}' to be valid"

    def test_invalid_skill_names(self, importer):
        """Test that invalid skill names fail validation."""
        invalid_names = [
            "test@skill",  # @ symbol
            "test skill",  # Space
            "test.skill",  # Dot
            "test/skill",  # Slash
            "test\\skill",  # Backslash
            "test!skill",  # Exclamation
            "test#skill",  # Hash
            "test$skill",  # Dollar sign
            "",  # Empty string
        ]

        for name in invalid_names:
            # Should not match the pattern
            assert not importer.VALID_NAME_PATTERN.match(name), f"Expected '{name}' to be invalid"
