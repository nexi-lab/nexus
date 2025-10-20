"""Unit tests for skill manager."""

import tempfile
from pathlib import Path

import pytest

from nexus.skills.manager import SkillManager, SkillManagerError
from nexus.skills.parser import SkillParser
from nexus.skills.registry import SkillRegistry


# Mock filesystem for testing
class MockFilesystem:
    """Mock filesystem for testing manager."""

    def __init__(self):
        """Initialize with empty filesystem."""
        self._files: dict[str, bytes] = {}
        self._directories: set[str] = set()

    def exists(self, path: str) -> bool:
        """Check if path exists."""
        if path in self._files:
            return True
        # Normalize path for directory check
        search_path = path if path.endswith("/") else path + "/"
        return any(f.startswith(search_path) for f in list(self._files) + list(self._directories))

    def is_directory(self, path: str) -> bool:
        """Check if path is a directory."""
        if path in self._files:
            return False
        search_path = path if path.endswith("/") else path + "/"
        return any(f.startswith(search_path) for f in self._files)

    def list(self, path: str, recursive: bool = False) -> list[str]:
        """List files in directory."""
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
        return files

    def read(self, path: str) -> bytes:
        """Read file content."""
        if path not in self._files:
            raise FileNotFoundError(f"File not found: {path}")
        return self._files[path]

    def write(self, path: str, content: bytes) -> None:
        """Write file content."""
        self._files[path] = content

    def mkdir(self, path: str, parents: bool = False) -> None:
        """Create directory."""
        if not path.endswith("/"):
            path += "/"
        self._directories.add(path)


# Sample skill for testing fork
EXISTING_SKILL = b"""---
name: existing-skill
description: Existing skill for testing
version: 1.0.0
author: Test Author
requires:
  - dependency-skill
---

# Existing Skill

This is the content of the existing skill.
"""


@pytest.mark.asyncio
async def test_manager_initialization() -> None:
    """Test SkillManager initialization."""
    fs = MockFilesystem()
    registry = SkillRegistry(filesystem=fs)
    manager = SkillManager(filesystem=fs, registry=registry)

    assert manager._filesystem is fs
    assert manager._registry is registry


@pytest.mark.asyncio
async def test_create_skill_basic_template() -> None:
    """Test creating a skill from basic template."""
    fs = MockFilesystem()
    manager = SkillManager(filesystem=fs)

    path = await manager.create_skill(
        "test-skill", description="Test skill", template="basic", tier="agent"
    )

    assert path == "/workspace/.nexus/skills/test-skill/SKILL.md"
    assert fs.exists(path)

    # Parse the created skill
    content = fs.read(path).decode("utf-8")
    parser = SkillParser()
    skill = parser.parse_content(content)

    assert skill.metadata.name == "test-skill"
    assert skill.metadata.description == "Test skill"
    assert skill.metadata.version == "1.0.0"
    assert "# test-skill" in skill.content
    assert "Test skill" in skill.content


@pytest.mark.asyncio
async def test_create_skill_with_author() -> None:
    """Test creating a skill with author."""
    fs = MockFilesystem()
    manager = SkillManager(filesystem=fs)

    await manager.create_skill(
        "test-skill",
        description="Test skill",
        template="basic",
        tier="agent",
        author="Alice",
    )

    path = "/workspace/.nexus/skills/test-skill/SKILL.md"
    content = fs.read(path).decode("utf-8")
    parser = SkillParser()
    skill = parser.parse_content(content)

    assert skill.metadata.author == "Alice"


@pytest.mark.asyncio
async def test_create_skill_different_templates() -> None:
    """Test creating skills from different templates."""
    fs = MockFilesystem()
    manager = SkillManager(filesystem=fs)

    templates = [
        "basic",
        "data-analysis",
        "code-generation",
        "document-processing",
        "api-integration",
    ]

    for i, template in enumerate(templates):
        path = await manager.create_skill(
            f"skill-{i}",
            description=f"Skill from {template}",
            template=template,
            tier="agent",
        )

        content = fs.read(path).decode("utf-8")
        assert f"Skill from {template}" in content


@pytest.mark.asyncio
async def test_create_skill_already_exists() -> None:
    """Test that creating duplicate skill raises error."""
    fs = MockFilesystem()
    manager = SkillManager(filesystem=fs)

    # Create first skill
    await manager.create_skill("test-skill", description="Test", tier="agent")

    # Try to create same skill again
    with pytest.raises(SkillManagerError, match="already exists"):
        await manager.create_skill("test-skill", description="Test", tier="agent")


@pytest.mark.asyncio
async def test_create_skill_invalid_name() -> None:
    """Test that invalid skill names raise error."""
    fs = MockFilesystem()
    manager = SkillManager(filesystem=fs)

    with pytest.raises(SkillManagerError, match="must be alphanumeric"):
        await manager.create_skill("invalid name!", description="Test")


@pytest.mark.asyncio
async def test_create_skill_invalid_tier() -> None:
    """Test that invalid tier raises error."""
    fs = MockFilesystem()
    manager = SkillManager(filesystem=fs)

    with pytest.raises(SkillManagerError, match="Invalid tier"):
        await manager.create_skill("test-skill", description="Test", tier="invalid")


@pytest.mark.asyncio
async def test_create_skill_different_tiers() -> None:
    """Test creating skills in different tiers."""
    fs = MockFilesystem()
    manager = SkillManager(filesystem=fs)

    tiers = ["agent", "tenant", "system"]
    expected_paths = [
        "/workspace/.nexus/skills/skill-agent/SKILL.md",
        "/shared/skills/skill-tenant/SKILL.md",
        "/system/skills/skill-system/SKILL.md",
    ]

    for tier, expected_path in zip(tiers, expected_paths, strict=False):
        path = await manager.create_skill(f"skill-{tier}", description="Test", tier=tier)
        assert path == expected_path
        assert fs.exists(path)


@pytest.mark.asyncio
async def test_fork_skill() -> None:
    """Test forking an existing skill."""
    fs = MockFilesystem()

    # Add existing skill
    fs.write("/workspace/.nexus/skills/existing-skill/SKILL.md", EXISTING_SKILL)

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["agent"])

    manager = SkillManager(filesystem=fs, registry=registry)

    # Fork the skill
    path = await manager.fork_skill("existing-skill", "forked-skill", tier="agent")

    assert path == "/workspace/.nexus/skills/forked-skill/SKILL.md"
    assert fs.exists(path)

    # Parse forked skill
    content = fs.read(path).decode("utf-8")
    parser = SkillParser()
    skill = parser.parse_content(content)

    # Check metadata
    assert skill.metadata.name == "forked-skill"
    assert skill.metadata.description == "Existing skill for testing"
    assert skill.metadata.version == "1.1.0"  # Version incremented

    # Check lineage tracking
    assert "forked_from" in skill.content or "forked_from" in content
    assert "existing-skill" in content

    # Check content is preserved
    assert "This is the content of the existing skill" in skill.content


@pytest.mark.asyncio
async def test_fork_skill_with_author() -> None:
    """Test forking with custom author."""
    fs = MockFilesystem()
    fs.write("/workspace/.nexus/skills/existing-skill/SKILL.md", EXISTING_SKILL)

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["agent"])

    manager = SkillManager(filesystem=fs, registry=registry)

    await manager.fork_skill("existing-skill", "forked-skill", tier="agent", author="Bob")

    content = fs.read("/workspace/.nexus/skills/forked-skill/SKILL.md").decode("utf-8")
    assert "author: Bob" in content


@pytest.mark.asyncio
async def test_fork_skill_preserves_dependencies() -> None:
    """Test that forking preserves dependencies."""
    fs = MockFilesystem()
    fs.write("/workspace/.nexus/skills/existing-skill/SKILL.md", EXISTING_SKILL)

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["agent"])

    manager = SkillManager(filesystem=fs, registry=registry)

    await manager.fork_skill("existing-skill", "forked-skill")

    content = fs.read("/workspace/.nexus/skills/forked-skill/SKILL.md").decode("utf-8")
    parser = SkillParser()
    skill = parser.parse_content(content)

    assert "dependency-skill" in skill.metadata.requires


@pytest.mark.asyncio
async def test_fork_skill_not_found() -> None:
    """Test that forking non-existent skill raises error."""
    fs = MockFilesystem()
    registry = SkillRegistry(filesystem=fs)
    manager = SkillManager(filesystem=fs, registry=registry)

    with pytest.raises(SkillManagerError, match="not found"):
        await manager.fork_skill("nonexistent", "forked-skill")


@pytest.mark.asyncio
async def test_fork_skill_target_exists() -> None:
    """Test that forking to existing name raises error."""
    fs = MockFilesystem()
    fs.write("/workspace/.nexus/skills/existing-skill/SKILL.md", EXISTING_SKILL)
    fs.write("/workspace/.nexus/skills/forked-skill/SKILL.md", EXISTING_SKILL)

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["agent"])

    manager = SkillManager(filesystem=fs, registry=registry)

    with pytest.raises(SkillManagerError, match="already exists"):
        await manager.fork_skill("existing-skill", "forked-skill")


@pytest.mark.asyncio
async def test_fork_skill_invalid_target_name() -> None:
    """Test that forking to invalid name raises error."""
    fs = MockFilesystem()
    fs.write("/workspace/.nexus/skills/existing-skill/SKILL.md", EXISTING_SKILL)

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["agent"])

    manager = SkillManager(filesystem=fs, registry=registry)

    with pytest.raises(SkillManagerError, match="must be alphanumeric"):
        await manager.fork_skill("existing-skill", "invalid name!")


@pytest.mark.asyncio
async def test_publish_skill() -> None:
    """Test publishing a skill from agent to tenant tier."""
    fs = MockFilesystem()
    fs.write("/workspace/.nexus/skills/my-skill/SKILL.md", EXISTING_SKILL)

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["agent"])

    manager = SkillManager(filesystem=fs, registry=registry)

    # Publish to tenant tier
    path = await manager.publish_skill("existing-skill", source_tier="agent", target_tier="tenant")

    assert path == "/shared/skills/existing-skill/SKILL.md"
    assert fs.exists(path)

    # Parse published skill
    content = fs.read(path).decode("utf-8")
    parser = SkillParser()
    skill = parser.parse_content(content)

    # Check metadata
    assert skill.metadata.name == "existing-skill"
    assert skill.metadata.description == "Existing skill for testing"

    # Check publication tracking
    assert "published_from" in content
    assert "agent" in content
    assert "published_at" in content


@pytest.mark.asyncio
async def test_publish_skill_preserves_content() -> None:
    """Test that publishing preserves skill content."""
    fs = MockFilesystem()
    fs.write("/workspace/.nexus/skills/my-skill/SKILL.md", EXISTING_SKILL)

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["agent"])

    manager = SkillManager(filesystem=fs, registry=registry)

    await manager.publish_skill("existing-skill", source_tier="agent", target_tier="tenant")

    content = fs.read("/shared/skills/existing-skill/SKILL.md").decode("utf-8")
    assert "This is the content of the existing skill" in content


@pytest.mark.asyncio
async def test_publish_skill_not_found() -> None:
    """Test that publishing non-existent skill raises error."""
    fs = MockFilesystem()
    registry = SkillRegistry(filesystem=fs)
    manager = SkillManager(filesystem=fs, registry=registry)

    with pytest.raises(SkillManagerError, match="not found"):
        await manager.publish_skill("nonexistent", source_tier="agent", target_tier="tenant")


@pytest.mark.asyncio
async def test_publish_skill_invalid_source_tier() -> None:
    """Test that invalid source tier raises error."""
    fs = MockFilesystem()
    manager = SkillManager(filesystem=fs)

    with pytest.raises(SkillManagerError, match="Invalid source tier"):
        await manager.publish_skill("skill", source_tier="invalid", target_tier="tenant")


@pytest.mark.asyncio
async def test_publish_skill_invalid_target_tier() -> None:
    """Test that invalid target tier raises error."""
    fs = MockFilesystem()
    manager = SkillManager(filesystem=fs)

    with pytest.raises(SkillManagerError, match="Invalid target tier"):
        await manager.publish_skill("skill", source_tier="agent", target_tier="invalid")


@pytest.mark.asyncio
async def test_create_skill_local_filesystem() -> None:
    """Test creating skill on local filesystem."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Override tier paths for testing
        original_paths = SkillRegistry.TIER_PATHS.copy()
        SkillRegistry.TIER_PATHS = {"agent": f"{tmpdir}/agent/"}

        try:
            manager = SkillManager()

            path = await manager.create_skill(
                "local-skill", description="Local test skill", tier="agent"
            )

            # Check file exists on local filesystem
            assert Path(path).exists()

            # Parse content
            content = Path(path).read_text()
            assert "local-skill" in content
            assert "Local test skill" in content

        finally:
            SkillRegistry.TIER_PATHS = original_paths


@pytest.mark.asyncio
async def test_fork_skill_local_filesystem() -> None:
    """Test forking skill on local filesystem."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Override tier paths
        original_paths = SkillRegistry.TIER_PATHS.copy()
        SkillRegistry.TIER_PATHS = {"agent": f"{tmpdir}/agent/"}

        try:
            # Create source skill
            source_dir = Path(tmpdir) / "agent" / "source-skill"
            source_dir.mkdir(parents=True)
            (source_dir / "SKILL.md").write_bytes(EXISTING_SKILL)

            registry = SkillRegistry()
            await registry.discover(tiers=["agent"])

            manager = SkillManager(registry=registry)

            # Fork skill
            path = await manager.fork_skill("existing-skill", "forked-local", tier="agent")

            # Check file exists
            assert Path(path).exists()

            # Verify content
            content = Path(path).read_text()
            assert "forked-local" in content
            assert "forked_from" in content

        finally:
            SkillRegistry.TIER_PATHS = original_paths
