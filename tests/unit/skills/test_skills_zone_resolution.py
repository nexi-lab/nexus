"""Targeted tests for skills manager/registry zone resolution.

Tests the code paths being modified:
- ROOT_ZONE_ID → normalize_zone_id() replacement
- SkillRegistry.get_tier_paths() with various contexts
- SkillManager zone resolution in create/publish flows

Uses nexus.bricks.skills submodules directly (canonical location).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from nexus.bricks.skills.registry import SkillRegistry
from nexus.bricks.skills.testing import FakeOperationContext, InMemorySkillFilesystem

# ── SkillRegistry zone resolution tests ──────────────────────────


class TestSkillRegistryGetTierPaths:
    """Tests for SkillRegistry.get_tier_paths() zone resolution."""

    def test_no_context_returns_defaults(self) -> None:
        paths = SkillRegistry.get_tier_paths(None)
        assert "system" in paths
        assert "user" in paths
        assert "zone" in paths
        assert paths["system"] == "/skill/"

    def test_with_zone_context(self) -> None:
        ctx = FakeOperationContext(zone_id="tenant-1")
        paths = SkillRegistry.get_tier_paths(ctx)
        assert paths["zone"] == "/zone/tenant-1/skill/"

    def test_with_user_and_zone_context(self) -> None:
        ctx = FakeOperationContext(zone_id="t1", user_id="alice")
        paths = SkillRegistry.get_tier_paths(ctx)
        assert paths["zone"] == "/zone/t1/skill/"
        assert paths["personal"] == "/zone/t1/user/alice/skill/"
        assert paths["user"] == "/zone/t1/user/alice/skill/"

    def test_empty_string_zone_defaults_to_root(self) -> None:
        """When zone_id is empty string, should use ROOT_ZONE_ID default."""
        ctx = FakeOperationContext(zone_id="", user_id="bob")
        paths = SkillRegistry.get_tier_paths(ctx)
        # Empty zone_id treated as falsy -> uses ROOT_ZONE_ID ("root")
        assert "/zone/root/" in paths["zone"]

    def test_system_path_always_present(self) -> None:
        ctx = FakeOperationContext(zone_id="any")
        paths = SkillRegistry.get_tier_paths(ctx)
        assert paths["system"] == "/skill/"


class TestSkillRegistryConstruction:
    """Tests for SkillRegistry construction."""

    def test_basic_construction(self) -> None:
        fs = MagicMock(spec=InMemorySkillFilesystem)
        registry = SkillRegistry(fs)
        assert registry is not None

    def test_construction_with_rebac(self) -> None:
        fs = MagicMock(spec=InMemorySkillFilesystem)
        mock_rebac = MagicMock()
        registry = SkillRegistry(fs, rebac_manager=mock_rebac)
        assert registry is not None


class TestSkillRegistryTierPriority:
    """Tests for tier priority ordering."""

    def test_personal_highest_priority(self) -> None:
        assert SkillRegistry.TIER_PRIORITY["personal"] > SkillRegistry.TIER_PRIORITY["zone"]
        assert SkillRegistry.TIER_PRIORITY["zone"] > SkillRegistry.TIER_PRIORITY["system"]

    def test_user_is_alias_for_personal(self) -> None:
        assert SkillRegistry.TIER_PRIORITY["user"] == SkillRegistry.TIER_PRIORITY["personal"]
