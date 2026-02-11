"""Tests for SandboxSecurityProfile (Issue #1000).

Covers factory methods, immutability, Docker kwargs conversion,
trust tier mapping, equality, and hashing.
"""

from __future__ import annotations

import pytest

from nexus.sandbox.security_profile import (
    DEFAULT_EGRESS_ALLOWLIST,
    TRUST_TIER_PROFILE_MAP,
    SandboxSecurityProfile,
)

# ---------------------------------------------------------------------------
# Factory method value tests
# ---------------------------------------------------------------------------


class TestStrictProfile:
    """Strict profile: maximum isolation for untrusted agents."""

    @pytest.fixture()
    def profile(self) -> SandboxSecurityProfile:
        return SandboxSecurityProfile.strict()

    def test_name(self, profile: SandboxSecurityProfile) -> None:
        assert profile.name == "strict"

    def test_network_none(self, profile: SandboxSecurityProfile) -> None:
        assert profile.network_mode == "none"

    def test_no_capabilities_added(self, profile: SandboxSecurityProfile) -> None:
        assert profile.capabilities_add == ()

    def test_drops_all_capabilities(self, profile: SandboxSecurityProfile) -> None:
        assert profile.capabilities_drop == ("ALL",)

    def test_docker_default_seccomp(self, profile: SandboxSecurityProfile) -> None:
        assert profile.seccomp_profile is None

    def test_read_only_root(self, profile: SandboxSecurityProfile) -> None:
        assert profile.read_only_root is True

    def test_tmpfs_tmp(self, profile: SandboxSecurityProfile) -> None:
        tmpfs_dict = dict(profile.tmpfs_mounts)
        assert "/tmp" in tmpfs_dict
        assert "size=10m" in tmpfs_dict["/tmp"]

    def test_memory_limit(self, profile: SandboxSecurityProfile) -> None:
        assert profile.memory_limit == "256m"

    def test_cpu_limit(self, profile: SandboxSecurityProfile) -> None:
        assert profile.cpu_limit == 0.5

    def test_python_no_bytecode(self, profile: SandboxSecurityProfile) -> None:
        env_dict = dict(profile.env_vars)
        assert env_dict.get("PYTHONDONTWRITEBYTECODE") == "1"

    def test_no_egress(self, profile: SandboxSecurityProfile) -> None:
        assert profile.allowed_egress_domains == ()

    def test_no_fuse(self, profile: SandboxSecurityProfile) -> None:
        assert profile.allow_fuse is False


class TestStandardProfile:
    """Standard profile: balanced isolation for skill-builders and default."""

    @pytest.fixture()
    def profile(self) -> SandboxSecurityProfile:
        return SandboxSecurityProfile.standard()

    def test_name(self, profile: SandboxSecurityProfile) -> None:
        assert profile.name == "standard"

    def test_network_none(self, profile: SandboxSecurityProfile) -> None:
        assert profile.network_mode == "none"

    def test_no_capabilities_added(self, profile: SandboxSecurityProfile) -> None:
        assert profile.capabilities_add == ()

    def test_drops_all_capabilities(self, profile: SandboxSecurityProfile) -> None:
        assert profile.capabilities_drop == ("ALL",)

    def test_read_only_root(self, profile: SandboxSecurityProfile) -> None:
        assert profile.read_only_root is True

    def test_tmpfs_tmp_larger(self, profile: SandboxSecurityProfile) -> None:
        tmpfs_dict = dict(profile.tmpfs_mounts)
        assert "/tmp" in tmpfs_dict
        assert "size=50m" in tmpfs_dict["/tmp"]

    def test_memory_limit(self, profile: SandboxSecurityProfile) -> None:
        assert profile.memory_limit == "512m"

    def test_cpu_limit(self, profile: SandboxSecurityProfile) -> None:
        assert profile.cpu_limit == 1.0

    def test_python_no_bytecode(self, profile: SandboxSecurityProfile) -> None:
        env_dict = dict(profile.env_vars)
        assert env_dict.get("PYTHONDONTWRITEBYTECODE") == "1"

    def test_egress_allowlist(self, profile: SandboxSecurityProfile) -> None:
        assert profile.allowed_egress_domains == DEFAULT_EGRESS_ALLOWLIST
        assert "api.openai.com" in profile.allowed_egress_domains
        assert "api.anthropic.com" in profile.allowed_egress_domains

    def test_fuse_allowed(self, profile: SandboxSecurityProfile) -> None:
        assert profile.allow_fuse is True


class TestPermissiveProfile:
    """Permissive profile: minimal restrictions for trusted agents."""

    @pytest.fixture()
    def profile(self) -> SandboxSecurityProfile:
        return SandboxSecurityProfile.permissive()

    def test_name(self, profile: SandboxSecurityProfile) -> None:
        assert profile.name == "permissive"

    def test_default_bridge_network(self, profile: SandboxSecurityProfile) -> None:
        assert profile.network_mode is None

    def test_no_capabilities_added(self, profile: SandboxSecurityProfile) -> None:
        assert profile.capabilities_add == ()

    def test_drops_all_capabilities(self, profile: SandboxSecurityProfile) -> None:
        assert profile.capabilities_drop == ("ALL",)

    def test_fuse_allowed(self, profile: SandboxSecurityProfile) -> None:
        assert profile.allow_fuse is True

    def test_writable_root(self, profile: SandboxSecurityProfile) -> None:
        assert profile.read_only_root is False

    def test_no_tmpfs(self, profile: SandboxSecurityProfile) -> None:
        assert profile.tmpfs_mounts == ()

    def test_memory_limit(self, profile: SandboxSecurityProfile) -> None:
        assert profile.memory_limit == "1g"

    def test_cpu_limit(self, profile: SandboxSecurityProfile) -> None:
        assert profile.cpu_limit == 2.0

    def test_no_extra_env_vars(self, profile: SandboxSecurityProfile) -> None:
        assert profile.env_vars == ()

    def test_all_egress_allowed(self, profile: SandboxSecurityProfile) -> None:
        assert profile.allowed_egress_domains == ("*",)


# ---------------------------------------------------------------------------
# No profile ever adds SYS_ADMIN
# ---------------------------------------------------------------------------


def test_strict_no_sys_admin_in_capabilities_add():
    """Strict profile must not declare SYS_ADMIN in capabilities_add."""
    profile = SandboxSecurityProfile.strict()
    assert "SYS_ADMIN" not in profile.capabilities_add


def test_fuse_profiles_get_sys_admin_via_to_docker_kwargs():
    """FUSE-enabled profiles get SYS_ADMIN injected in to_docker_kwargs(), not in capabilities_add."""
    for factory in [SandboxSecurityProfile.standard, SandboxSecurityProfile.permissive]:
        profile = factory()
        # capabilities_add is empty (SYS_ADMIN is injected by to_docker_kwargs)
        assert "SYS_ADMIN" not in profile.capabilities_add
        # But it appears in the Docker kwargs
        kwargs = profile.to_docker_kwargs()
        assert "SYS_ADMIN" in kwargs.get("cap_add", [])


# ---------------------------------------------------------------------------
# Immutability (frozen dataclass)
# ---------------------------------------------------------------------------


class TestImmutability:
    def test_cannot_set_name(self) -> None:
        profile = SandboxSecurityProfile.strict()
        with pytest.raises(AttributeError):
            profile.name = "hacked"  # type: ignore[misc]

    def test_cannot_set_network_mode(self) -> None:
        profile = SandboxSecurityProfile.strict()
        with pytest.raises(AttributeError):
            profile.network_mode = "bridge"  # type: ignore[misc]

    def test_cannot_set_memory_limit(self) -> None:
        profile = SandboxSecurityProfile.strict()
        with pytest.raises(AttributeError):
            profile.memory_limit = "8g"  # type: ignore[misc]

    def test_cannot_set_cpu_limit(self) -> None:
        profile = SandboxSecurityProfile.strict()
        with pytest.raises(AttributeError):
            profile.cpu_limit = 100.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Equality and hashing
# ---------------------------------------------------------------------------


class TestEqualityAndHashing:
    def test_same_factory_produces_equal_profiles(self) -> None:
        assert SandboxSecurityProfile.strict() == SandboxSecurityProfile.strict()

    def test_different_factories_not_equal(self) -> None:
        assert SandboxSecurityProfile.strict() != SandboxSecurityProfile.standard()

    def test_strict_standard_permissive_all_different(self) -> None:
        profiles = {
            SandboxSecurityProfile.strict(),
            SandboxSecurityProfile.standard(),
            SandboxSecurityProfile.permissive(),
        }
        assert len(profiles) == 3

    def test_hashable(self) -> None:
        """Profiles can be used as dict keys or set members."""
        profile = SandboxSecurityProfile.strict()
        d = {profile: "test"}
        assert d[SandboxSecurityProfile.strict()] == "test"


# ---------------------------------------------------------------------------
# to_docker_kwargs() conversion
# ---------------------------------------------------------------------------


class TestToDockerKwargs:
    def test_strict_kwargs(self) -> None:
        kwargs = SandboxSecurityProfile.strict().to_docker_kwargs()

        assert kwargs["network_mode"] == "none"
        assert "cap_add" not in kwargs  # no FUSE → no SYS_ADMIN added
        assert kwargs["cap_drop"] == ["ALL"]
        assert kwargs["read_only"] is True
        assert kwargs["tmpfs"] == {"/tmp": "size=10m,noexec"}
        assert kwargs["mem_limit"] == "256m"
        assert kwargs["cpu_quota"] == 50000  # 0.5 * 100000
        assert kwargs["cpu_period"] == 100000
        assert kwargs["environment"]["PYTHONDONTWRITEBYTECODE"] == "1"
        # Strict: no-new-privileges:true (no FUSE)
        assert "no-new-privileges:true" in kwargs["security_opt"]
        # No apparmor=unconfined (Docker's default AppArmor is active)
        for opt in kwargs["security_opt"]:
            assert "apparmor=unconfined" not in opt

    def test_standard_kwargs(self) -> None:
        kwargs = SandboxSecurityProfile.standard().to_docker_kwargs()

        assert kwargs["network_mode"] == "none"
        # Standard: drops ALL, then adds back SYS_ADMIN for FUSE
        assert "SYS_ADMIN" in kwargs["cap_add"]
        assert kwargs["cap_drop"] == ["ALL"]
        assert kwargs["read_only"] is True
        assert kwargs["tmpfs"] == {"/tmp": "size=50m"}
        assert kwargs["mem_limit"] == "512m"
        assert kwargs["cpu_quota"] == 100000  # 1.0 * 100000
        assert kwargs["environment"]["PYTHONDONTWRITEBYTECODE"] == "1"
        # Standard: no-new-privileges:false (needed for FUSE sudo)
        assert "no-new-privileges:false" in kwargs["security_opt"]
        # No apparmor=unconfined
        for opt in kwargs["security_opt"]:
            assert "apparmor=unconfined" not in opt

    def test_permissive_kwargs(self) -> None:
        kwargs = SandboxSecurityProfile.permissive().to_docker_kwargs()

        # network_mode not set when None (caller decides)
        assert "network_mode" not in kwargs
        # Permissive: drops ALL, then adds back SYS_ADMIN for FUSE
        assert "SYS_ADMIN" in kwargs["cap_add"]
        assert kwargs["cap_drop"] == ["ALL"]
        assert "read_only" not in kwargs  # writable root
        assert "tmpfs" not in kwargs or kwargs["tmpfs"] == {}
        assert kwargs["mem_limit"] == "1g"
        assert kwargs["cpu_quota"] == 200000  # 2.0 * 100000
        assert "environment" not in kwargs  # no extra env vars
        # Permissive: no-new-privileges:false (needed for FUSE sudo)
        assert "no-new-privileges:false" in kwargs["security_opt"]

    def test_custom_seccomp_profile_path(self) -> None:
        profile = SandboxSecurityProfile(
            name="custom",
            network_mode="none",
            seccomp_profile="/path/to/seccomp.json",
            memory_limit="512m",
            cpu_limit=1.0,
        )
        kwargs = profile.to_docker_kwargs()
        assert "seccomp=/path/to/seccomp.json" in kwargs["security_opt"]

    def test_no_new_privileges_strict(self) -> None:
        """Strict profile sets no-new-privileges:true (no FUSE)."""
        kwargs = SandboxSecurityProfile.strict().to_docker_kwargs()
        assert "no-new-privileges:true" in kwargs["security_opt"]

    def test_no_new_privileges_fuse_profiles(self) -> None:
        """FUSE profiles set no-new-privileges:false (needed for sudo/fusermount)."""
        for factory in [SandboxSecurityProfile.standard, SandboxSecurityProfile.permissive]:
            kwargs = factory().to_docker_kwargs()
            assert "no-new-privileges:false" in kwargs["security_opt"]

    def test_apparmor_unconfined_never_set(self) -> None:
        """No profile ever sets apparmor=unconfined."""
        for factory in [
            SandboxSecurityProfile.strict,
            SandboxSecurityProfile.standard,
            SandboxSecurityProfile.permissive,
        ]:
            kwargs = factory().to_docker_kwargs()
            for opt in kwargs["security_opt"]:
                assert "apparmor=unconfined" not in opt


# ---------------------------------------------------------------------------
# Trust tier mapping
# ---------------------------------------------------------------------------


class TestTrustTierMapping:
    def test_untrusted_maps_to_strict(self) -> None:
        profile = SandboxSecurityProfile.from_trust_tier("UntrustedAgent")
        assert profile.name == "strict"

    def test_skill_builder_maps_to_standard(self) -> None:
        profile = SandboxSecurityProfile.from_trust_tier("SkillBuilder")
        assert profile.name == "standard"

    def test_impersonated_maps_to_permissive(self) -> None:
        profile = SandboxSecurityProfile.from_trust_tier("ImpersonatedUser")
        assert profile.name == "permissive"

    def test_none_defaults_to_standard(self) -> None:
        profile = SandboxSecurityProfile.from_trust_tier(None)
        assert profile.name == "standard"

    def test_unknown_agent_defaults_to_standard(self) -> None:
        profile = SandboxSecurityProfile.from_trust_tier("SomeRandomAgent")
        assert profile.name == "standard"

    def test_map_covers_all_provisioning_types(self) -> None:
        """Ensure TRUST_TIER_PROFILE_MAP includes all standard agent types."""
        expected_agents = {"UntrustedAgent", "SkillBuilder", "ImpersonatedUser"}
        assert set(TRUST_TIER_PROFILE_MAP.keys()) == expected_agents

    def test_from_trust_tier_returns_new_instances(self) -> None:
        """Each call returns a fresh profile (no shared mutable state)."""
        p1 = SandboxSecurityProfile.from_trust_tier("UntrustedAgent")
        p2 = SandboxSecurityProfile.from_trust_tier("UntrustedAgent")
        assert p1 == p2
        assert p1 is not p2

    def test_case_insensitive_lookup(self) -> None:
        """Trust tier lookup must be case-insensitive to prevent bypass."""
        assert SandboxSecurityProfile.from_trust_tier("untrustedagent").name == "strict"
        assert SandboxSecurityProfile.from_trust_tier("UNTRUSTEDAGENT").name == "strict"
        assert SandboxSecurityProfile.from_trust_tier("skillbuilder").name == "standard"
        assert SandboxSecurityProfile.from_trust_tier("IMPERSONATEDUSER").name == "permissive"
