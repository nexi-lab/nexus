"""Unit tests for skill exporter."""

import io
import json
import tempfile
import zipfile
from pathlib import Path

import pytest

from nexus.skills.exporter import SkillExporter, SkillExportError
from nexus.skills.models import Skill, SkillMetadata
from nexus.skills.registry import SkillRegistry


# Mock filesystem for testing
class MockFilesystem:
    """Mock filesystem for testing."""

    def __init__(self, files: dict[str, bytes]):
        self._files = files

    def exists(self, path: str) -> bool:
        if path in self._files:
            return True
        # Handle paths that may or may not end with /
        search_path = path if path.endswith("/") else path + "/"
        return any(f.startswith(search_path) for f in self._files)

    def is_directory(self, path: str, context=None) -> bool:
        if path in self._files:
            return False
        # Handle paths that may or may not end with /
        search_path = path if path.endswith("/") else path + "/"
        return any(f.startswith(search_path) for f in self._files)

    def list(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        prefix: str | None = None,
        show_parsed: bool = True,
        context=None,
    ) -> list[str] | list[dict]:
        if not path.endswith("/"):
            path += "/"
        files = []
        for file_path in self._files:
            if file_path.startswith(path):
                if recursive:
                    files.append(file_path)
                else:
                    rel_path = file_path[len(path) :]
                    if "/" not in rel_path:
                        files.append(file_path)
        # Return list of dicts if details=True, otherwise list of strings
        if details:
            return [{"path": f, "size": len(self._files[f])} for f in files]
        return files

    def read(self, path: str, context=None, return_metadata: bool = False) -> bytes:
        if path not in self._files:
            raise FileNotFoundError(f"File not found: {path}")
        if return_metadata:
            return {"content": self._files[path]}
        return self._files[path]


# Sample skills
SKILL_SIMPLE = b"""---
name: simple-skill
description: A simple skill
version: 1.0.0
---

# Simple Skill

This is a simple skill for testing.
"""

SKILL_WITH_DEPS = b"""---
name: complex-skill
description: A complex skill with dependencies
version: 2.0.0
requires:
  - simple-skill
---

# Complex Skill

This skill depends on simple-skill.
"""


@pytest.mark.asyncio
async def test_export_skill_to_bytes() -> None:
    """Test exporting a skill to bytes."""
    fs = MockFilesystem(
        {
            "/skills/user/simple-skill/SKILL.md": SKILL_SIMPLE,
        }
    )

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    exporter = SkillExporter(registry)
    zip_bytes = await exporter.export_skill("simple-skill", output_path=None)

    assert zip_bytes is not None
    assert len(zip_bytes) > 0

    # Verify it's a valid zip
    zip_buffer = io.BytesIO(zip_bytes)
    with zipfile.ZipFile(zip_buffer, "r") as zf:
        assert "simple-skill/SKILL.md" in zf.namelist()
        assert "manifest.json" in zf.namelist()


@pytest.mark.asyncio
async def test_export_skill_to_file() -> None:
    """Test exporting a skill to a file."""
    fs = MockFilesystem(
        {
            "/skills/user/simple-skill/SKILL.md": SKILL_SIMPLE,
        }
    )

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    exporter = SkillExporter(registry)

    with tempfile.TemporaryDirectory() as tmpdir:
        output_path = Path(tmpdir) / "simple-skill.zip"
        result = await exporter.export_skill("simple-skill", output_path=str(output_path))

        assert result is None  # Returns None when writing to file
        assert output_path.exists()

        # Verify zip contents
        with zipfile.ZipFile(output_path, "r") as zf:
            assert "simple-skill/SKILL.md" in zf.namelist()
            assert "manifest.json" in zf.namelist()


@pytest.mark.asyncio
async def test_export_skill_with_dependencies() -> None:
    """Test exporting a skill with its dependencies."""
    fs = MockFilesystem(
        {
            "/skills/user/simple-skill/SKILL.md": SKILL_SIMPLE,
            "/skills/user/complex-skill/SKILL.md": SKILL_WITH_DEPS,
        }
    )

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    exporter = SkillExporter(registry)
    zip_bytes = await exporter.export_skill(
        "complex-skill", output_path=None, include_dependencies=True
    )

    # Verify both skills are in the zip
    zip_buffer = io.BytesIO(zip_bytes)
    with zipfile.ZipFile(zip_buffer, "r") as zf:
        namelist = zf.namelist()
        assert "complex-skill/SKILL.md" in namelist
        assert "simple-skill/SKILL.md" in namelist


@pytest.mark.asyncio
async def test_export_skill_without_dependencies() -> None:
    """Test exporting a skill without its dependencies."""
    fs = MockFilesystem(
        {
            "/skills/user/simple-skill/SKILL.md": SKILL_SIMPLE,
            "/skills/user/complex-skill/SKILL.md": SKILL_WITH_DEPS,
        }
    )

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    exporter = SkillExporter(registry)
    zip_bytes = await exporter.export_skill(
        "complex-skill", output_path=None, include_dependencies=False
    )

    # Verify only the main skill is in the zip
    zip_buffer = io.BytesIO(zip_bytes)
    with zipfile.ZipFile(zip_buffer, "r") as zf:
        namelist = zf.namelist()
        assert "complex-skill/SKILL.md" in namelist
        assert "simple-skill/SKILL.md" not in namelist


@pytest.mark.asyncio
async def test_export_manifest_content() -> None:
    """Test that manifest.json contains correct information."""
    fs = MockFilesystem(
        {
            "/skills/user/simple-skill/SKILL.md": SKILL_SIMPLE,
        }
    )

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    exporter = SkillExporter(registry)
    zip_bytes = await exporter.export_skill("simple-skill")

    # Read manifest
    zip_buffer = io.BytesIO(zip_bytes)
    with zipfile.ZipFile(zip_buffer, "r") as zf:
        manifest_json = zf.read("manifest.json").decode("utf-8")
        manifest = json.loads(manifest_json)

        assert manifest["name"] == "simple-skill"
        assert manifest["version"] == "1.0.0"
        assert "simple-skill/SKILL.md" in manifest["files"]
        assert manifest["total_size_bytes"] > 0


@pytest.mark.asyncio
async def test_export_skill_not_found() -> None:
    """Test that exporting non-existent skill raises error."""
    registry = SkillRegistry()
    exporter = SkillExporter(registry)

    with pytest.raises(SkillExportError, match="Skill not found"):
        await exporter.export_skill("nonexistent")


@pytest.mark.asyncio
async def test_validate_export() -> None:
    """Test validating export without creating package."""
    fs = MockFilesystem(
        {
            "/skills/user/simple-skill/SKILL.md": SKILL_SIMPLE,
        }
    )

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    exporter = SkillExporter(registry)

    valid, msg, size = await exporter.validate_export("simple-skill")

    assert valid is True
    assert "valid" in msg.lower()
    assert size > 0


@pytest.mark.asyncio
async def test_validate_export_size_limit() -> None:
    """Test that validation detects size limit violations."""
    # Create a large skill (>8MB)
    large_content = b"""---
name: large-skill
description: A very large skill
---

""" + (b"# " + b"A" * 9 * 1024 * 1024)

    fs = MockFilesystem(
        {
            "/skills/user/large-skill/SKILL.md": large_content,
        }
    )

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    exporter = SkillExporter(registry)

    # Test with claude format to trigger size limit check
    valid, msg, size = await exporter.validate_export("large-skill", format="claude")

    assert valid is False
    assert "exceeds" in msg.lower()
    assert size > 8 * 1024 * 1024


@pytest.mark.asyncio
async def test_import_skill() -> None:
    """Test importing skills from .zip package."""
    # Create a skill package
    fs = MockFilesystem(
        {
            "/skills/user/simple-skill/SKILL.md": SKILL_SIMPLE,
        }
    )

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    exporter = SkillExporter(registry)

    with tempfile.TemporaryDirectory() as tmpdir:
        # Export to zip
        export_path = Path(tmpdir) / "export.zip"
        await exporter.export_skill("simple-skill", output_path=str(export_path))

        # Import to a different directory
        import_dir = Path(tmpdir) / "imported"
        imported = await exporter.import_skill(
            str(export_path), tier="user", output_dir=str(import_dir)
        )

        assert "simple-skill" in imported
        assert (import_dir / "simple-skill" / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_import_skill_from_bytes() -> None:
    """Test importing skills from bytes (file-like object)."""
    fs = MockFilesystem(
        {
            "/skills/user/simple-skill/SKILL.md": SKILL_SIMPLE,
        }
    )

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    exporter = SkillExporter(registry)

    # Export to bytes
    zip_bytes = await exporter.export_skill("simple-skill")

    with tempfile.TemporaryDirectory() as tmpdir:
        # Import from bytes
        import_dir = Path(tmpdir) / "imported"
        zip_buffer = io.BytesIO(zip_bytes)
        imported = await exporter.import_skill(zip_buffer, tier="user", output_dir=str(import_dir))

        assert "simple-skill" in imported
        assert (import_dir / "simple-skill" / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_reconstruct_skill_md() -> None:
    """Test reconstructing SKILL.md from Skill object."""
    metadata = SkillMetadata(
        name="test-skill",
        description="Test skill",
        version="1.0.0",
        author="Test Author",
        requires=["dep-1", "dep-2"],
    )
    skill = Skill(
        metadata=metadata,
        content="# Test Content\n\nSome content here.",
    )

    registry = SkillRegistry()
    exporter = SkillExporter(registry)

    skill_md = exporter._reconstruct_skill_md(skill)

    # Should have frontmatter
    assert "---" in skill_md
    assert "name: test-skill" in skill_md
    assert "description: Test skill" in skill_md
    assert "version: 1.0.0" in skill_md
    assert "author: Test Author" in skill_md
    assert "requires:" in skill_md
    assert "- dep-1" in skill_md

    # Should have content
    assert "# Test Content" in skill_md
    assert "Some content here." in skill_md


@pytest.mark.asyncio
async def test_calculate_skill_size() -> None:
    """Test calculating skill size."""
    metadata = SkillMetadata(
        name="test-skill",
        description="Test",
    )
    skill = Skill(
        metadata=metadata,
        content="# Content",
    )

    registry = SkillRegistry()
    exporter = SkillExporter(registry)

    size = await exporter._calculate_skill_size(skill)

    assert size > 0
    # Size should include both frontmatter and content
    assert size > len("# Content")
