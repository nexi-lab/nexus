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

    def is_directory(self, path: str, context=None) -> bool:
        """Check if path is a directory."""
        if path in self._files:
            return False
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

        # Return list of dicts if details=True, otherwise list of strings
        if details:
            return [{"path": f, "size": len(self._files[f])} for f in files]
        return files

    def read(self, path: str, context=None, return_metadata: bool = False) -> bytes:
        """Read file content."""
        if path not in self._files:
            raise FileNotFoundError(f"File not found: {path}")
        if return_metadata:
            return {"content": self._files[path]}
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
        "test-skill", description="Test skill", template="basic", tier="user"
    )

    assert path == "/skills/user/test-skill/SKILL.md"
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
        tier="user",
        author="Alice",
    )

    path = "/skills/user/test-skill/SKILL.md"
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
            tier="user",
        )

        content = fs.read(path).decode("utf-8")
        assert f"Skill from {template}" in content


@pytest.mark.asyncio
async def test_create_skill_already_exists() -> None:
    """Test that creating duplicate skill raises error."""
    fs = MockFilesystem()
    manager = SkillManager(filesystem=fs)

    # Create first skill
    await manager.create_skill("test-skill", description="Test", tier="user")

    # Try to create same skill again
    with pytest.raises(SkillManagerError, match="already exists"):
        await manager.create_skill("test-skill", description="Test", tier="user")


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

    tiers = ["user", "tenant", "system"]
    expected_paths = [
        "/skills/user/skill-user/SKILL.md",
        "/skills/tenant/skill-tenant/SKILL.md",
        "/skill/skill-system/SKILL.md",
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
    fs.write("/skills/user/existing-skill/SKILL.md", EXISTING_SKILL)

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    manager = SkillManager(filesystem=fs, registry=registry)

    # Fork the skill
    path = await manager.fork_skill("existing-skill", "forked-skill", tier="user")

    assert path == "/skills/user/forked-skill/SKILL.md"
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
    fs.write("/skills/user/existing-skill/SKILL.md", EXISTING_SKILL)

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    manager = SkillManager(filesystem=fs, registry=registry)

    await manager.fork_skill("existing-skill", "forked-skill", tier="user", author="Bob")

    content = fs.read("/skills/user/forked-skill/SKILL.md").decode("utf-8")
    assert "author: Bob" in content


@pytest.mark.asyncio
async def test_fork_skill_preserves_dependencies() -> None:
    """Test that forking preserves dependencies."""
    fs = MockFilesystem()
    fs.write("/skills/user/existing-skill/SKILL.md", EXISTING_SKILL)

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    manager = SkillManager(filesystem=fs, registry=registry)

    await manager.fork_skill("existing-skill", "forked-skill", tier="user")

    content = fs.read("/skills/user/forked-skill/SKILL.md").decode("utf-8")
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
        await manager.fork_skill("nonexistent", "forked-skill", tier="user")


@pytest.mark.asyncio
async def test_fork_skill_target_exists() -> None:
    """Test that forking to existing name raises error."""
    fs = MockFilesystem()
    fs.write("/skills/user/existing-skill/SKILL.md", EXISTING_SKILL)
    fs.write("/skills/user/forked-skill/SKILL.md", EXISTING_SKILL)

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    manager = SkillManager(filesystem=fs, registry=registry)

    with pytest.raises(SkillManagerError, match="already exists"):
        await manager.fork_skill("existing-skill", "forked-skill", tier="user")


@pytest.mark.asyncio
async def test_fork_skill_invalid_target_name() -> None:
    """Test that forking to invalid name raises error."""
    fs = MockFilesystem()
    fs.write("/skills/user/existing-skill/SKILL.md", EXISTING_SKILL)

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    manager = SkillManager(filesystem=fs, registry=registry)

    with pytest.raises(SkillManagerError, match="must be alphanumeric"):
        await manager.fork_skill("existing-skill", "invalid name!", tier="user")


@pytest.mark.asyncio
async def test_publish_skill() -> None:
    """Test publishing a skill from user to tenant tier."""
    fs = MockFilesystem()
    fs.write("/skills/user/my-skill/SKILL.md", EXISTING_SKILL)

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    manager = SkillManager(filesystem=fs, registry=registry)

    # Publish to tenant tier
    path = await manager.publish_skill("existing-skill", source_tier="user", target_tier="tenant")

    assert path == "/skills/tenant/existing-skill/SKILL.md"
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
    assert "user" in content
    assert "published_at" in content


@pytest.mark.asyncio
async def test_publish_skill_preserves_content() -> None:
    """Test that publishing preserves skill content."""
    fs = MockFilesystem()
    fs.write("/skills/user/my-skill/SKILL.md", EXISTING_SKILL)

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    manager = SkillManager(filesystem=fs, registry=registry)

    await manager.publish_skill("existing-skill", source_tier="user", target_tier="tenant")

    content = fs.read("/skills/tenant/existing-skill/SKILL.md").decode("utf-8")
    assert "This is the content of the existing skill" in content


@pytest.mark.asyncio
async def test_publish_skill_not_found() -> None:
    """Test that publishing non-existent skill raises error."""
    fs = MockFilesystem()
    registry = SkillRegistry(filesystem=fs)
    manager = SkillManager(filesystem=fs, registry=registry)

    with pytest.raises(SkillManagerError, match="not found"):
        await manager.publish_skill("nonexistent", source_tier="user", target_tier="tenant")


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
        await manager.publish_skill("skill", source_tier="user", target_tier="invalid")


@pytest.mark.asyncio
async def test_create_skill_local_filesystem() -> None:
    """Test creating skill on local filesystem."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Override tier paths for testing
        original_paths = SkillRegistry.TIER_PATHS.copy()
        SkillRegistry.TIER_PATHS = {"user": f"{tmpdir}/user/"}

        try:
            manager = SkillManager()

            path = await manager.create_skill(
                "local-skill", description="Local test skill", tier="user"
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
        SkillRegistry.TIER_PATHS = {"user": f"{tmpdir}/user/"}

        try:
            # Create source skill
            source_dir = Path(tmpdir) / "user" / "source-skill"
            source_dir.mkdir(parents=True)
            (source_dir / "SKILL.md").write_bytes(EXISTING_SKILL)

            registry = SkillRegistry()
            await registry.discover(tiers=["user"])

            manager = SkillManager(registry=registry)

            # Fork skill
            path = await manager.fork_skill("existing-skill", "forked-local", tier="user")

            # Check file exists
            assert Path(path).exists()

            # Verify content
            content = Path(path).read_text()
            assert "forked-local" in content
            assert "forked_from" in content

        finally:
            SkillRegistry.TIER_PATHS = original_paths


@pytest.mark.asyncio
async def test_search_skills_basic() -> None:
    """Test basic skill search by description."""
    fs = MockFilesystem()

    # Create skills with different descriptions
    skill1 = b"""---
name: code-analyzer
description: Analyzes code quality and structure
---
Content
"""
    skill2 = b"""---
name: data-processor
description: Processes and transforms data
---
Content
"""
    skill3 = b"""---
name: code-generator
description: Generates code from specifications
---
Content
"""

    fs.write("/skills/user/code-analyzer/SKILL.md", skill1)
    fs.write("/skills/user/data-processor/SKILL.md", skill2)
    fs.write("/skills/user/code-generator/SKILL.md", skill3)

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    manager = SkillManager(filesystem=fs, registry=registry)

    # Search for "code"
    results = await manager.search_skills("code")

    assert len(results) > 0
    skill_names = [name for name, score in results]
    assert "code-analyzer" in skill_names
    assert "code-generator" in skill_names


@pytest.mark.asyncio
async def test_search_skills_phrase_match() -> None:
    """Test search with phrase matching."""
    fs = MockFilesystem()

    skill1 = b"""---
name: skill1
description: Analyzes code quality
---
Content
"""
    skill2 = b"""---
name: skill2
description: Analyzes data quality
---
Content
"""

    fs.write("/skills/user/skill1/SKILL.md", skill1)
    fs.write("/skills/user/skill2/SKILL.md", skill2)

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    manager = SkillManager(filesystem=fs, registry=registry)

    # Search for exact phrase
    results = await manager.search_skills("code quality")

    assert len(results) > 0
    # skill1 should rank higher (exact phrase match)
    assert results[0][0] == "skill1"


@pytest.mark.asyncio
async def test_search_skills_name_match() -> None:
    """Test search that matches skill name."""
    fs = MockFilesystem()

    skill = b"""---
name: data-analyzer
description: General purpose tool
---
Content
"""

    fs.write("/skills/user/data-analyzer/SKILL.md", skill)

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    manager = SkillManager(filesystem=fs, registry=registry)

    # Search for name
    results = await manager.search_skills("data")

    assert len(results) == 1
    assert results[0][0] == "data-analyzer"
    assert results[0][1] > 0  # Should have positive score


@pytest.mark.asyncio
async def test_search_skills_tier_filter() -> None:
    """Test search filtered by tier."""
    fs = MockFilesystem()

    agent_skill = b"""---
name: agent-skill
description: Agent skill for testing
---
Content
"""
    tenant_skill = b"""---
name: tenant-skill
description: Tenant skill for testing
---
Content
"""

    fs.write("/skills/user/agent-skill/SKILL.md", agent_skill)
    fs.write("/skills/tenant/tenant-skill/SKILL.md", tenant_skill)

    registry = SkillRegistry(filesystem=fs)
    await registry.discover()

    manager = SkillManager(filesystem=fs, registry=registry)

    # Search only user tier
    results = await manager.search_skills("testing", tier="user")

    assert len(results) == 1
    assert results[0][0] == "agent-skill"


@pytest.mark.asyncio
async def test_search_skills_with_limit() -> None:
    """Test search with result limit."""
    fs = MockFilesystem()

    # Create many skills
    for i in range(20):
        skill = f"""---
name: skill-{i}
description: Test skill number {i}
---
Content
""".encode()
        fs.write(f"/skills/user/skill-{i}/SKILL.md", skill)

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    manager = SkillManager(filesystem=fs, registry=registry)

    # Search with limit
    results = await manager.search_skills("test", limit=5)

    assert len(results) == 5


@pytest.mark.asyncio
async def test_search_skills_no_matches() -> None:
    """Test search with no matching skills."""
    fs = MockFilesystem()

    skill = b"""---
name: test-skill
description: Something completely different
---
Content
"""

    fs.write("/skills/user/test-skill/SKILL.md", skill)

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    manager = SkillManager(filesystem=fs, registry=registry)

    # Search for non-matching term
    results = await manager.search_skills("nonexistent")

    assert len(results) == 0


@pytest.mark.asyncio
async def test_search_skills_scoring() -> None:
    """Test that search results are properly scored and sorted."""
    fs = MockFilesystem()

    # Skill with exact phrase match should rank highest
    skill1 = b"""---
name: skill1
description: Machine learning model training
---
Content
"""
    # Skill with partial match should rank lower
    skill2 = b"""---
name: skill2
description: This skill does machine things and learning activities
---
Content
"""
    # Skill with only one word match should rank lowest
    skill3 = b"""---
name: skill3
description: Training documentation generator
---
Content
"""

    fs.write("/skills/user/skill1/SKILL.md", skill1)
    fs.write("/skills/user/skill2/SKILL.md", skill2)
    fs.write("/skills/user/skill3/SKILL.md", skill3)

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    manager = SkillManager(filesystem=fs, registry=registry)

    # Search for phrase
    results = await manager.search_skills("machine learning")

    # Only skill1 and skill2 match (skill3 doesn't have "machine" or "learning")
    assert len(results) == 2

    # Verify ranking (exact phrase match first)
    assert results[0][0] == "skill1"  # Exact phrase in description
    assert results[0][1] > results[1][1]  # Higher score than skill2
