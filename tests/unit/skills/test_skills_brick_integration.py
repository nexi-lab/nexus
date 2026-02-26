"""Brick-level integration test for the Skills module (Issue #2035).

Tests the full lifecycle (create → publish → discover → subscribe → load)
using in-memory fakes from skills/testing.py — zero kernel dependencies.
"""

import base64
import io
import json
import zipfile

import pytest

from nexus.bricks.skills.package_service import SkillPackageService
from nexus.bricks.skills.service import SkillService
from nexus.bricks.skills.testing import (
    FakeOperationContext,
    InMemorySkillFilesystem,
    StubSkillPermissions,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fs():
    return InMemorySkillFilesystem()


@pytest.fixture
def perms():
    return StubSkillPermissions()


@pytest.fixture
def ctx():
    return FakeOperationContext(user_id="alice", zone_id="acme")


@pytest.fixture
def svc(fs, perms):
    return SkillService(fs=fs, perms=perms)


@pytest.fixture
def pkg_svc(fs, perms, svc):
    return SkillPackageService(fs=fs, perms=perms, skill_service=svc)


SKILL_PATH = "/zone/acme/user/alice/skill/code-review/"

# ---------------------------------------------------------------------------
# Full Lifecycle
# ---------------------------------------------------------------------------


class TestSkillsBrickLifecycle:
    """Test create → discover → subscribe → load → export → import."""

    def test_full_lifecycle(self, fs, svc, pkg_svc, ctx):
        """End-to-end lifecycle with protocol fakes."""
        # 1. Create skill (simulated by seeding filesystem)
        fs.seed_skill(SKILL_PATH, name="code-review", description="Code review assistant")

        # 2. Discover — should find the skill
        skills = svc.discover(ctx, filter="owned")
        assert len(skills) == 1
        assert skills[0].name == "code-review"

        # 3. Subscribe
        result = svc.subscribe(SKILL_PATH, ctx)
        assert result is True

        # 4. Verify subscription
        skills = svc.discover(ctx, filter="subscribed")
        assert len(skills) == 1
        assert skills[0].is_subscribed is True

        # 5. Load full content
        content = svc.load(SKILL_PATH, ctx)
        assert content.name == "code-review"
        assert "# Test" in content.content

        # 6. Export
        export_result = pkg_svc.export(skill_path=SKILL_PATH, context=ctx)
        assert export_result["success"] is True
        assert "zip_data" in export_result

        # 7. Import to different user
        ctx2 = FakeOperationContext(user_id="bob", zone_id="acme")
        import_result = pkg_svc.import_skill(
            zip_data=export_result["zip_data"],
            context=ctx2,
        )
        assert "code-review" in import_result["imported_skills"]

        # 8. Unsubscribe
        result = svc.unsubscribe(SKILL_PATH, ctx)
        assert result is True

        skills = svc.discover(ctx, filter="subscribed")
        assert len(skills) == 0


class TestDiscoverFilters:
    """Test discover with various filter modes."""

    def test_filter_owned(self, fs, svc, ctx):
        fs.seed_skill(SKILL_PATH, name="my-skill")
        skills = svc.discover(ctx, filter="owned")
        assert len(skills) == 1

    def test_filter_subscribed_empty(self, svc, ctx):
        skills = svc.discover(ctx, filter="subscribed")
        assert skills == []

    def test_filter_public(self, fs, svc, perms, ctx):
        path = "/zone/other/user/bob/skill/public-skill/"
        fs.seed_skill(path, name="public-skill")
        perms.tuples.append(
            {
                "subject_type": "role",
                "subject_id": "public",
                "relation": "direct_viewer",
                "object_type": "file",
                "object_id": path.rstrip("/"),
            }
        )
        skills = svc.discover(ctx, filter="public")
        assert len(skills) == 1
        assert skills[0].is_public is True

    def test_filter_all(self, fs, svc, ctx):
        fs.seed_skill(SKILL_PATH, name="my-skill")
        skills = svc.discover(ctx, filter="all")
        assert len(skills) >= 1


class TestShareUnshare:
    """Test skill sharing and unsharing."""

    def test_share_public(self, fs, svc, perms, ctx):
        fs.seed_skill(SKILL_PATH, name="code-review")
        tuple_id = svc.share(SKILL_PATH, "public", ctx)
        assert tuple_id.startswith("tuple-")

    def test_share_with_user(self, fs, svc, perms, ctx):
        fs.seed_skill(SKILL_PATH, name="code-review")
        tuple_id = svc.share(SKILL_PATH, "user:bob", ctx)
        assert tuple_id.startswith("tuple-")

    def test_share_with_zone(self, fs, svc, perms, ctx):
        fs.seed_skill(SKILL_PATH, name="code-review")
        tuple_id = svc.share(SKILL_PATH, "zone", ctx)
        assert tuple_id.startswith("tuple-")

    def test_share_invalid_format(self, fs, svc, perms, ctx):
        fs.seed_skill(SKILL_PATH, name="code-review")
        from nexus.bricks.skills.exceptions import SkillValidationError

        with pytest.raises(SkillValidationError, match="Invalid share_with"):
            svc.share(SKILL_PATH, "invalid", ctx)


class TestContextValidation:
    """Test context validation."""

    def test_none_context_raises(self, svc):
        from nexus.bricks.skills.exceptions import SkillValidationError

        with pytest.raises(SkillValidationError, match="Context"):
            svc.discover(None)

    def test_missing_user_id_raises(self, svc):
        from nexus.bricks.skills.exceptions import SkillValidationError

        ctx = FakeOperationContext(user_id="", zone_id="acme")
        with pytest.raises(SkillValidationError, match="Context"):
            svc.discover(ctx)


class TestPromptContext:
    """Test get_prompt_context."""

    def test_prompt_context_for_subscribed_skills(self, fs, svc, ctx):
        fs.seed_skill(SKILL_PATH, name="code-review", description="Review code")
        svc.subscribe(SKILL_PATH, ctx)

        prompt = svc.get_prompt_context(ctx)
        assert prompt.count == 1
        assert "<available_skills>" in prompt.xml
        assert "code-review" in prompt.xml

    def test_prompt_context_empty(self, svc, ctx):
        prompt = svc.get_prompt_context(ctx)
        assert prompt.count == 0


class TestPackageValidation:
    """Test validate_zip."""

    def test_valid_package(self, pkg_svc, ctx):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            manifest = {"version": "1.0", "skill_path": "/skill/test/"}
            zf.writestr("manifest.json", json.dumps(manifest))
            zf.writestr("SKILL.md", "---\nname: test\n---\n# Test")
        zip_data = base64.b64encode(buf.getvalue()).decode()

        result = pkg_svc.validate_zip(zip_data=zip_data, context=ctx)
        assert result["valid"] is True
        assert "test" in result["skills_found"]

    def test_invalid_zip(self, pkg_svc, ctx):
        result = pkg_svc.validate_zip(zip_data=base64.b64encode(b"not a zip").decode(), context=ctx)
        assert result["valid"] is False


class TestMetadataCache:
    """Test request-scoped metadata cache (Issue #2035, Phase 5.1)."""

    def test_metadata_cached_within_request(self, fs, svc, ctx):
        fs.seed_skill(SKILL_PATH, name="cached-skill")
        # First call populates cache
        svc._load_skill_metadata(SKILL_PATH, ctx)
        # Modify underlying file
        fs._files[f"{SKILL_PATH}SKILL.md"] = b"---\nname: changed\n---\n# Changed"
        # Second call returns cached value
        metadata = svc._load_skill_metadata(SKILL_PATH, ctx)
        assert metadata.get("name") == "cached-skill"

    def test_cache_cleared_on_new_request(self, fs, svc, ctx):
        fs.seed_skill(SKILL_PATH, name="cached-skill")
        svc._load_skill_metadata(SKILL_PATH, ctx)
        # Clear cache (simulates new request)
        svc.clear_metadata_cache()
        fs._files[f"{SKILL_PATH}SKILL.md"] = b"---\nname: changed\n---\n# Changed"
        metadata = svc._load_skill_metadata(SKILL_PATH, ctx)
        assert metadata.get("name") == "changed"


class TestTTLSubscriptionCache:
    """Test TTL-based subscription cache (Issue #2035, Phase 5.2)."""

    def test_cache_returns_data_within_ttl(self, fs, svc, ctx):
        import yaml

        fs.seed_skill(SKILL_PATH, name="test")
        subs = {"subscribed_skills": [SKILL_PATH]}
        fs.sys_write(
            "/zone/acme/user/alice/skill/.subscribed.yaml",
            yaml.dump(subs).encode(),
        )

        result1 = svc._load_subscriptions(ctx)
        assert len(result1) == 1

        # Modify file — cache should still return old data
        fs.sys_write(
            "/zone/acme/user/alice/skill/.subscribed.yaml",
            yaml.dump({"subscribed_skills": []}).encode(),
        )
        result2 = svc._load_subscriptions(ctx)
        assert len(result2) == 1  # Cached
