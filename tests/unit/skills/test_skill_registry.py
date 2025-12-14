"""Unit tests for skill registry."""

import tempfile
from pathlib import Path

import pytest

from nexus.skills.models import Skill, SkillMetadata
from nexus.skills.registry import (
    SkillDependencyError,
    SkillNotFoundError,
    SkillRegistry,
)


# Mock filesystem for testing
class MockFilesystem:
    """Mock filesystem for testing registry."""

    def __init__(self, files: dict[str, bytes]):
        """Initialize with file path -> content mapping."""
        self._files = files

    def exists(self, path: str) -> bool:
        """Check if path exists."""
        # Check if it's a file or directory
        if path in self._files:
            return True
        # Check if any file starts with this path (directory)
        # Handle paths that may or may not end with /
        search_path = path if path.endswith("/") else path + "/"
        return any(f.startswith(search_path) for f in self._files)

    def is_directory(self, path: str, context=None) -> bool:
        """Check if path is a directory."""
        if path in self._files:
            return False  # It's a file
        # Check if any file is under this path
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
        """List files in directory."""
        if not path.endswith("/"):
            path += "/"

        files = []
        for file_path in self._files:
            if file_path.startswith(path):
                if recursive:
                    files.append(file_path)
                else:
                    # Only direct children
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
            # Minimal shape needed by protocol; tests here only expect bytes.
            return {"content": self._files[path]}
        return self._files[path]


# Sample skill content
SKILL_1 = b"""---
name: skill-1
description: First test skill
---

# Skill 1

Content for skill 1.
"""

SKILL_2 = b"""---
name: skill-2
description: Second test skill
requires:
  - skill-1
---

# Skill 2

Content for skill 2.
"""

SKILL_3 = b"""---
name: skill-3
description: Third test skill
requires:
  - skill-1
  - skill-2
---

# Skill 3

Content for skill 3.
"""

SKILL_CIRCULAR_A = b"""---
name: skill-a
description: Skill A
requires:
  - skill-b
---

Content A
"""

SKILL_CIRCULAR_B = b"""---
name: skill-b
description: Skill B
requires:
  - skill-a
---

Content B
"""


@pytest.mark.asyncio
async def test_registry_initialization() -> None:
    """Test SkillRegistry initialization."""
    registry = SkillRegistry()

    assert len(registry.list_skills()) == 0
    assert repr(registry).startswith("SkillRegistry")


@pytest.mark.asyncio
async def test_discover_from_local_filesystem() -> None:
    """Test discovering skills from local filesystem."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create skill files
        user_dir = Path(tmpdir) / "user"
        user_dir.mkdir()

        skill1_file = user_dir / "skill-1" / "SKILL.md"
        skill1_file.parent.mkdir()
        skill1_file.write_bytes(SKILL_1)

        skill2_file = user_dir / "skill-2" / "SKILL.md"
        skill2_file.parent.mkdir()
        skill2_file.write_bytes(SKILL_2)

        # Temporarily override tier path for testing
        original_paths = SkillRegistry.TIER_PATHS.copy()
        SkillRegistry.TIER_PATHS = {"user": str(user_dir)}

        try:
            registry = SkillRegistry()
            count = await registry.discover(tiers=["user"])

            assert count == 2
            assert "skill-1" in registry.list_skills()
            assert "skill-2" in registry.list_skills()
        finally:
            SkillRegistry.TIER_PATHS = original_paths


@pytest.mark.asyncio
async def test_discover_from_mock_filesystem() -> None:
    """Test discovering skills from mock filesystem."""
    fs = MockFilesystem(
        {
            "/skills/user/skill-1/SKILL.md": SKILL_1,
            "/skills/user/skill-2/SKILL.md": SKILL_2,
        }
    )

    registry = SkillRegistry(filesystem=fs)
    count = await registry.discover(tiers=["user"])

    assert count == 2
    assert "skill-1" in registry.list_skills()
    assert "skill-2" in registry.list_skills()


@pytest.mark.asyncio
async def test_discover_tier_priority() -> None:
    """Test that higher priority tiers override lower priority."""
    # Skill with same name in different tiers
    user_skill = b"""---
name: duplicate-skill
description: User version
---
User content
"""

    tenant_skill = b"""---
name: duplicate-skill
description: Tenant version
---
Tenant content
"""

    fs = MockFilesystem(
        {
            "/skills/user/duplicate-skill/SKILL.md": user_skill,
            "/skills/tenant/duplicate-skill/SKILL.md": tenant_skill,
        }
    )

    registry = SkillRegistry(filesystem=fs)
    await registry.discover()  # Discover from all tiers

    # User tier has higher priority
    metadata = registry.get_metadata("duplicate-skill")
    assert metadata.description == "User version"
    assert metadata.tier == "user"


@pytest.mark.asyncio
async def test_get_skill_lazy_loading() -> None:
    """Test that skills are loaded lazily (on-demand)."""
    fs = MockFilesystem(
        {
            "/skills/user/skill-1/SKILL.md": SKILL_1,
        }
    )

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    # After discovery, skill should be in metadata index but not cache
    assert "skill-1" in registry.list_skills()
    assert "skill-1" not in registry._skill_cache

    # Get skill - should load full content
    skill = await registry.get_skill("skill-1")
    assert isinstance(skill, Skill)
    assert skill.metadata.name == "skill-1"
    assert "Content for skill 1" in skill.content

    # Now it should be in cache
    assert "skill-1" in registry._skill_cache


@pytest.mark.asyncio
async def test_get_skill_from_cache() -> None:
    """Test that subsequent get_skill calls use cache."""
    fs = MockFilesystem(
        {
            "/skills/user/skill-1/SKILL.md": SKILL_1,
        }
    )

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    # First call - loads from filesystem
    skill1 = await registry.get_skill("skill-1")

    # Second call - should use cache (same object)
    skill2 = await registry.get_skill("skill-1")

    assert skill1 is skill2


@pytest.mark.asyncio
async def test_get_skill_not_found() -> None:
    """Test that getting non-existent skill raises error."""
    registry = SkillRegistry()

    with pytest.raises(SkillNotFoundError, match="Skill not found: nonexistent"):
        await registry.get_skill("nonexistent")


@pytest.mark.asyncio
async def test_get_metadata() -> None:
    """Test getting skill metadata without loading full content."""
    fs = MockFilesystem(
        {
            "/skills/user/skill-1/SKILL.md": SKILL_1,
        }
    )

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    # Get metadata - should not load full content
    metadata = registry.get_metadata("skill-1")
    assert isinstance(metadata, SkillMetadata)
    assert metadata.name == "skill-1"
    assert metadata.description == "First test skill"

    # Should still not be in cache
    assert "skill-1" not in registry._skill_cache


@pytest.mark.asyncio
async def test_get_metadata_not_found() -> None:
    """Test that getting metadata for non-existent skill raises error."""
    registry = SkillRegistry()

    with pytest.raises(SkillNotFoundError, match="Skill not found"):
        registry.get_metadata("nonexistent")


@pytest.mark.asyncio
async def test_list_skills() -> None:
    """Test listing skills."""
    fs = MockFilesystem(
        {
            "/skills/user/skill-1/SKILL.md": SKILL_1,
            "/skills/user/skill-2/SKILL.md": SKILL_2,
        }
    )

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    skills = registry.list_skills()
    assert "skill-1" in skills
    assert "skill-2" in skills


@pytest.mark.asyncio
async def test_list_skills_by_tier() -> None:
    """Test listing skills filtered by tier."""
    fs = MockFilesystem(
        {
            "/skills/user/agent-skill/SKILL.md": SKILL_1,
            "/skills/tenant/tenant-skill/SKILL.md": SKILL_2,
        }
    )

    registry = SkillRegistry(filesystem=fs)
    await registry.discover()

    agent_skills = registry.list_skills(tier="user")
    assert len(agent_skills) == 1
    assert "skill-1" in agent_skills

    tenant_skills = registry.list_skills(tier="tenant")
    assert len(tenant_skills) == 1
    assert "skill-2" in tenant_skills


@pytest.mark.asyncio
async def test_list_skills_with_metadata() -> None:
    """Test listing skills with metadata included."""
    fs = MockFilesystem(
        {
            "/skills/user/skill-1/SKILL.md": SKILL_1,
        }
    )

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    metadata_list = registry.list_skills(include_metadata=True)
    assert len(metadata_list) == 1
    assert isinstance(metadata_list[0], SkillMetadata)
    assert metadata_list[0].name == "skill-1"


@pytest.mark.asyncio
async def test_resolve_dependencies() -> None:
    """Test resolving skill dependencies."""
    fs = MockFilesystem(
        {
            "/skills/user/skill-1/SKILL.md": SKILL_1,
            "/skills/user/skill-2/SKILL.md": SKILL_2,
            "/skills/user/skill-3/SKILL.md": SKILL_3,
        }
    )

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    # Skill 3 depends on skill-2, which depends on skill-1
    deps = await registry.resolve_dependencies("skill-3")

    # Dependencies should be in order: skill-1, skill-2, skill-3
    assert deps == ["skill-1", "skill-2", "skill-3"]


@pytest.mark.asyncio
async def test_resolve_dependencies_circular() -> None:
    """Test that circular dependencies are detected."""
    fs = MockFilesystem(
        {
            "/skills/user/skill-a/SKILL.md": SKILL_CIRCULAR_A,
            "/skills/user/skill-b/SKILL.md": SKILL_CIRCULAR_B,
        }
    )

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    # Should detect circular dependency
    with pytest.raises(SkillDependencyError, match="Circular dependency detected"):
        await registry.resolve_dependencies("skill-a")


@pytest.mark.asyncio
async def test_resolve_dependencies_missing() -> None:
    """Test that missing dependencies raise error."""
    fs = MockFilesystem(
        {
            "/skills/user/skill-2/SKILL.md": SKILL_2,
            # skill-1 is missing (but skill-2 requires it)
        }
    )

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    # Should raise error for missing dependency
    with pytest.raises(SkillNotFoundError, match="Skill not found: skill-1"):
        await registry.resolve_dependencies("skill-2")


@pytest.mark.asyncio
async def test_get_skill_with_dependencies() -> None:
    """Test loading skill with dependencies."""
    fs = MockFilesystem(
        {
            "/skills/user/skill-1/SKILL.md": SKILL_1,
            "/skills/user/skill-2/SKILL.md": SKILL_2,
        }
    )

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    # Load skill-2 with dependencies
    await registry.get_skill("skill-2", load_dependencies=True)

    # Both skills should now be in cache
    assert "skill-1" in registry._skill_cache
    assert "skill-2" in registry._skill_cache


@pytest.mark.asyncio
async def test_clear_cache() -> None:
    """Test clearing skill cache."""
    fs = MockFilesystem(
        {
            "/skills/user/skill-1/SKILL.md": SKILL_1,
        }
    )

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    # Load skill
    await registry.get_skill("skill-1")
    assert "skill-1" in registry._skill_cache

    # Clear cache
    registry.clear_cache()
    assert len(registry._skill_cache) == 0

    # Metadata should still be present
    assert "skill-1" in registry.list_skills()


@pytest.mark.asyncio
async def test_clear_all() -> None:
    """Test clearing all skills and caches."""
    fs = MockFilesystem(
        {
            "/skills/user/skill-1/SKILL.md": SKILL_1,
        }
    )

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])
    await registry.get_skill("skill-1")

    # Clear everything
    registry.clear()

    assert len(registry.list_skills()) == 0
    assert len(registry._skill_cache) == 0


@pytest.mark.asyncio
async def test_discover_nonexistent_tier() -> None:
    """Test discovering from nonexistent tier path."""
    fs = MockFilesystem({})

    registry = SkillRegistry(filesystem=fs)
    count = await registry.discover(tiers=["user"])

    # Should return 0, not raise error
    assert count == 0


@pytest.mark.asyncio
async def test_registry_repr() -> None:
    """Test registry string representation."""
    fs = MockFilesystem(
        {
            "/skills/user/skill-1/SKILL.md": SKILL_1,
        }
    )

    registry = SkillRegistry(filesystem=fs)
    await registry.discover(tiers=["user"])

    repr_str = repr(registry)
    assert "SkillRegistry" in repr_str
    assert "skills=1" in repr_str
    assert "cached=0" in repr_str

    # Load a skill and check again
    await registry.get_skill("skill-1")
    repr_str = repr(registry)
    assert "cached=1" in repr_str
