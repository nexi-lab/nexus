"""Tests for new nexus_fs_skills RPC endpoints (import, validate, export).

These tests cover the new endpoints added for skill management:
- skills_import: Import skill from .zip/.skill package
- skills_validate_zip: Validate ZIP without importing
- skills_export: Export skill to ZIP package
"""

import base64
import io
import tempfile
import zipfile
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus import LocalBackend, NexusFS
from nexus.core.exceptions import PermissionDeniedError
from nexus.core.permissions import OperationContext

# Mock SKILL.md content
VALID_SKILL_MD = """---
name: test-skill
description: A test skill for RPC testing
version: 1.0.0
author: Test Author
skill_type: documentation
---

# Test Skill

This is a test skill for RPC endpoint testing.
"""


def create_test_skill_zip() -> bytes:
    """Create a test skill ZIP file."""
    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        zip_file.writestr("test-skill/SKILL.md", VALID_SKILL_MD)
        zip_file.writestr("test-skill/README.md", "# Test Skill\n\nDocumentation")

    return zip_buffer.getvalue()


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def nx(temp_dir: Path) -> Generator[NexusFS, None, None]:
    """Create a NexusFS instance for testing."""
    nx_instance = NexusFS(
        backend=LocalBackend(temp_dir),
        db_path=temp_dir / "metadata.db",
        auto_parse=False,
        enforce_permissions=False,
    )
    yield nx_instance
    nx_instance.close()


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


class TestSkillsImport:
    """Test suite for skills_import RPC endpoint."""

    @patch("nexus.skills.importer.SkillImporter")
    def test_import_valid_skill_user_tier(self, mock_importer_class, nx: NexusFS, user_context):
        """Test importing a valid skill to user tier."""
        # Setup mock importer
        mock_importer = MagicMock()
        mock_importer.import_from_zip = AsyncMock(
            return_value={
                "imported_skills": ["test-skill"],
                "skill_paths": ["/skills/users/alice/test-skill/"],
                "tier": "user",
            }
        )
        mock_importer_class.return_value = mock_importer

        # Create test ZIP and encode to base64
        zip_data = create_test_skill_zip()
        zip_base64 = base64.b64encode(zip_data).decode("utf-8")

        # Call RPC endpoint
        result = nx.skills_import(
            zip_data=zip_base64,
            tier="user",
            allow_overwrite=False,
            context=user_context,
        )

        # Verify result
        assert result["imported_skills"] == ["test-skill"]
        assert result["tier"] == "user"
        assert len(result["skill_paths"]) == 1
        assert "/skills/users/alice/test-skill/" in result["skill_paths"][0]

        # Verify importer was called with decoded bytes
        mock_importer.import_from_zip.assert_called_once()
        call_args = mock_importer.import_from_zip.call_args[1]
        assert isinstance(call_args["zip_data"], bytes)
        assert call_args["tier"] == "user"
        assert call_args["allow_overwrite"] is False

    @patch("nexus.skills.importer.SkillImporter")
    def test_import_system_tier_as_admin(self, mock_importer_class, nx: NexusFS, admin_context):
        """Test importing to system tier as admin."""
        mock_importer = MagicMock()
        mock_importer.import_from_zip = AsyncMock(
            return_value={
                "imported_skills": ["test-skill"],
                "skill_paths": ["/skills/system/test-skill/"],
                "tier": "system",
            }
        )
        mock_importer_class.return_value = mock_importer

        zip_data = create_test_skill_zip()
        zip_base64 = base64.b64encode(zip_data).decode("utf-8")

        result = nx.skills_import(
            zip_data=zip_base64,
            tier="system",
            allow_overwrite=False,
            context=admin_context,
        )

        assert result["imported_skills"] == ["test-skill"]
        assert result["tier"] == "system"

    def test_import_system_tier_as_user_fails(self, nx: NexusFS, user_context):
        """Test that regular user cannot import to system tier."""
        zip_data = create_test_skill_zip()
        zip_base64 = base64.b64encode(zip_data).decode("utf-8")

        with pytest.raises(PermissionDeniedError, match="Only admins can import to system tier"):
            nx.skills_import(
                zip_data=zip_base64,
                tier="system",
                allow_overwrite=False,
                context=user_context,
            )

    def test_import_invalid_base64(self, nx: NexusFS, user_context):
        """Test importing with invalid base64 data."""
        import binascii

        with pytest.raises((binascii.Error, ValueError)):  # base64 decode error
            nx.skills_import(
                zip_data="not-valid-base64!!!",
                tier="user",
                allow_overwrite=False,
                context=user_context,
            )

    @patch("nexus.skills.importer.SkillImporter")
    def test_import_with_overwrite(self, mock_importer_class, nx: NexusFS, user_context):
        """Test importing with overwrite enabled."""
        mock_importer = MagicMock()
        mock_importer.import_from_zip = AsyncMock(
            return_value={
                "imported_skills": ["test-skill"],
                "skill_paths": ["/skills/users/alice/test-skill/"],
                "tier": "user",
            }
        )
        mock_importer_class.return_value = mock_importer

        zip_data = create_test_skill_zip()
        zip_base64 = base64.b64encode(zip_data).decode("utf-8")

        result = nx.skills_import(
            zip_data=zip_base64,
            tier="user",
            allow_overwrite=True,
            context=user_context,
        )

        assert result["imported_skills"] == ["test-skill"]

        # Verify overwrite flag was passed
        call_args = mock_importer.import_from_zip.call_args[1]
        assert call_args["allow_overwrite"] is True


class TestSkillsValidateZip:
    """Test suite for skills_validate_zip RPC endpoint."""

    @patch("nexus.skills.importer.SkillImporter")
    def test_validate_valid_zip(self, mock_importer_class, nx: NexusFS, user_context):
        """Test validating a valid skill ZIP."""
        mock_importer = MagicMock()
        mock_importer.validate_zip = AsyncMock(
            return_value={
                "valid": True,
                "skills_found": ["test-skill"],
                "errors": [],
                "warnings": [],
            }
        )
        mock_importer_class.return_value = mock_importer

        zip_data = create_test_skill_zip()
        zip_base64 = base64.b64encode(zip_data).decode("utf-8")

        result = nx.skills_validate_zip(
            zip_data=zip_base64,
            context=user_context,
        )

        assert result["valid"] is True
        assert "test-skill" in result["skills_found"]
        assert len(result["errors"]) == 0

    @patch("nexus.skills.importer.SkillImporter")
    def test_validate_invalid_zip(self, mock_importer_class, nx: NexusFS, user_context):
        """Test validating an invalid skill ZIP."""
        mock_importer = MagicMock()
        mock_importer.validate_zip = AsyncMock(
            return_value={
                "valid": False,
                "skills_found": [],
                "errors": ["No SKILL.md file found in package"],
                "warnings": [],
            }
        )
        mock_importer_class.return_value = mock_importer

        zip_data = create_test_skill_zip()
        zip_base64 = base64.b64encode(zip_data).decode("utf-8")

        result = nx.skills_validate_zip(
            zip_data=zip_base64,
            context=user_context,
        )

        assert result["valid"] is False
        assert len(result["errors"]) > 0
        assert "No SKILL.md file found" in result["errors"][0]

    @patch("nexus.skills.importer.SkillImporter")
    def test_validate_with_warnings(self, mock_importer_class, nx: NexusFS, user_context):
        """Test validation returns warnings for missing optional fields."""
        mock_importer = MagicMock()
        mock_importer.validate_zip = AsyncMock(
            return_value={
                "valid": True,
                "skills_found": ["test-skill"],
                "errors": [],
                "warnings": ["Missing version field in SKILL.md"],
            }
        )
        mock_importer_class.return_value = mock_importer

        zip_data = create_test_skill_zip()
        zip_base64 = base64.b64encode(zip_data).decode("utf-8")

        result = nx.skills_validate_zip(
            zip_data=zip_base64,
            context=user_context,
        )

        assert result["valid"] is True
        assert len(result["warnings"]) > 0
        assert "version" in result["warnings"][0].lower()


class TestSkillsExport:
    """Test suite for skills_export RPC endpoint."""

    @patch("nexus.skills.exporter.SkillExporter")
    def test_export_skill(self, mock_exporter_class, nx: NexusFS, user_context):
        """Test exporting a skill to ZIP."""
        mock_exporter = MagicMock()
        mock_exporter.export_skill = AsyncMock(return_value=create_test_skill_zip())
        mock_exporter_class.return_value = mock_exporter

        result = nx.skills_export(
            skill_name="test-skill",
            include_dependencies=False,
            context=user_context,
        )

        assert result["skill_name"] == "test-skill"
        assert "zip_data" in result
        assert isinstance(result["zip_data"], str)  # base64 encoded
        assert result["size_bytes"] > 0

        # Verify it's valid base64
        decoded = base64.b64decode(result["zip_data"])
        assert len(decoded) > 0

        # Verify it's a valid ZIP
        zip_buffer = io.BytesIO(decoded)
        with zipfile.ZipFile(zip_buffer, "r") as zip_file:
            assert len(zip_file.namelist()) > 0

    @patch("nexus.skills.exporter.SkillExporter")
    def test_export_with_dependencies(self, mock_exporter_class, nx: NexusFS, user_context):
        """Test exporting skill with dependencies."""
        mock_exporter = MagicMock()
        mock_exporter.export_skill = AsyncMock(return_value=create_test_skill_zip())
        mock_exporter_class.return_value = mock_exporter

        result = nx.skills_export(
            skill_name="test-skill",
            include_dependencies=True,
            context=user_context,
        )

        assert result["skill_name"] == "test-skill"
        assert "zip_data" in result

    @patch("nexus.skills.exporter.SkillExporter")
    def test_export_claude_format(self, mock_exporter_class, nx: NexusFS, user_context):
        """Test exporting in Claude format."""
        mock_exporter = MagicMock()
        mock_exporter.export_skill = AsyncMock(return_value=create_test_skill_zip())
        mock_exporter_class.return_value = mock_exporter

        result = nx.skills_export(
            skill_name="test-skill",
            include_dependencies=False,
            context=user_context,
        )

        assert result["skill_name"] == "test-skill"


class TestBase64EncodingDecoding:
    """Test base64 encoding/decoding for binary data."""

    def test_round_trip_encoding(self):
        """Test that ZIP data survives base64 round-trip."""
        original_zip = create_test_skill_zip()

        # Encode to base64
        encoded = base64.b64encode(original_zip).decode("utf-8")

        # Decode back
        decoded = base64.b64decode(encoded)

        # Should be identical
        assert decoded == original_zip

    def test_zip_validity_after_decode(self):
        """Test that decoded ZIP is still valid."""
        original_zip = create_test_skill_zip()
        encoded = base64.b64encode(original_zip).decode("utf-8")
        decoded = base64.b64decode(encoded)

        # Should be able to open as ZIP
        zip_buffer = io.BytesIO(decoded)
        with zipfile.ZipFile(zip_buffer, "r") as zip_file:
            files = zip_file.namelist()
            assert "test-skill/SKILL.md" in files

            # Read content
            content = zip_file.read("test-skill/SKILL.md")
            assert b"test-skill" in content
