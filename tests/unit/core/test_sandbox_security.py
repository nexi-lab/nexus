"""Negative security tests for sandbox isolation (Issue #1000).

Verifies that security invariants hold across all profiles and that
the Docker kwargs produced by profiles enforce proper isolation.
These tests are the "escape attempt" tests — they verify that the
security boundaries cannot be bypassed through configuration.
"""

from __future__ import annotations

import pytest

from nexus.sandbox.security_profile import (
    DEFAULT_EGRESS_ALLOWLIST,
    TRUST_TIER_PROFILE_MAP,
    SandboxSecurityProfile,
)

# ---------------------------------------------------------------------------
# No profile ever grants CAP_SYS_ADMIN in capabilities_add directly
# ---------------------------------------------------------------------------


class TestNoDirectSysAdmin:
    """SYS_ADMIN must only appear via to_docker_kwargs() FUSE injection,
    never in the profile's capabilities_add field."""

    @pytest.mark.parametrize(
        "factory",
        [
            SandboxSecurityProfile.strict,
            SandboxSecurityProfile.standard,
            SandboxSecurityProfile.permissive,
        ],
    )
    def test_sys_admin_not_in_capabilities_add(self, factory) -> None:
        profile = factory()
        assert "SYS_ADMIN" not in profile.capabilities_add

    def test_strict_never_gets_sys_admin_in_kwargs(self) -> None:
        kwargs = SandboxSecurityProfile.strict().to_docker_kwargs()
        assert "cap_add" not in kwargs or "SYS_ADMIN" not in kwargs.get("cap_add", [])

    def test_fuse_profiles_get_sys_admin_only_in_kwargs(self) -> None:
        for factory in [SandboxSecurityProfile.standard, SandboxSecurityProfile.permissive]:
            profile = factory()
            assert profile.allow_fuse is True
            assert "SYS_ADMIN" not in profile.capabilities_add
            kwargs = profile.to_docker_kwargs()
            assert "SYS_ADMIN" in kwargs["cap_add"]


# ---------------------------------------------------------------------------
# AppArmor is NEVER disabled
# ---------------------------------------------------------------------------


class TestApparmorNeverUnconfined:
    """Docker's default AppArmor profile must remain active for all profiles.
    The old insecure behavior of apparmor=unconfined is never set."""

    @pytest.mark.parametrize(
        "factory",
        [
            SandboxSecurityProfile.strict,
            SandboxSecurityProfile.standard,
            SandboxSecurityProfile.permissive,
        ],
    )
    def test_no_apparmor_unconfined(self, factory) -> None:
        kwargs = factory().to_docker_kwargs()
        for opt in kwargs.get("security_opt", []):
            assert "apparmor=unconfined" not in opt


# ---------------------------------------------------------------------------
# Strict profile: maximum lockdown
# ---------------------------------------------------------------------------


class TestStrictLockdown:
    """Strict profile must enforce maximum isolation."""

    @pytest.fixture()
    def kwargs(self) -> dict:
        return SandboxSecurityProfile.strict().to_docker_kwargs()

    def test_network_none(self, kwargs) -> None:
        assert kwargs["network_mode"] == "none"

    def test_drops_all_capabilities(self, kwargs) -> None:
        assert kwargs["cap_drop"] == ["ALL"]

    def test_no_capabilities_added(self, kwargs) -> None:
        assert "cap_add" not in kwargs

    def test_no_new_privileges_true(self, kwargs) -> None:
        assert "no-new-privileges:true" in kwargs["security_opt"]

    def test_read_only_filesystem(self, kwargs) -> None:
        assert kwargs["read_only"] is True

    def test_tmpfs_limited(self, kwargs) -> None:
        tmpfs = kwargs["tmpfs"]
        assert "/tmp" in tmpfs
        assert "noexec" in tmpfs["/tmp"]

    def test_memory_capped(self, kwargs) -> None:
        assert kwargs["mem_limit"] == "256m"

    def test_cpu_capped(self, kwargs) -> None:
        assert kwargs["cpu_quota"] == 50000  # 0.5 CPU

    def test_no_fuse(self) -> None:
        assert SandboxSecurityProfile.strict().allow_fuse is False


# ---------------------------------------------------------------------------
# Network isolation
# ---------------------------------------------------------------------------


class TestNetworkIsolation:
    """Profiles with network=none must have no direct internet access."""

    def test_strict_network_none(self) -> None:
        assert SandboxSecurityProfile.strict().network_mode == "none"

    def test_standard_network_none(self) -> None:
        assert SandboxSecurityProfile.standard().network_mode == "none"

    def test_permissive_uses_bridge(self) -> None:
        # Permissive uses Docker default bridge (network_mode=None)
        assert SandboxSecurityProfile.permissive().network_mode is None

    def test_strict_no_egress_domains(self) -> None:
        assert SandboxSecurityProfile.strict().allowed_egress_domains == ()

    def test_standard_limited_egress(self) -> None:
        domains = SandboxSecurityProfile.standard().allowed_egress_domains
        assert domains == DEFAULT_EGRESS_ALLOWLIST
        # Must not include wildcards
        assert "*" not in domains

    def test_permissive_wildcard_egress(self) -> None:
        assert SandboxSecurityProfile.permissive().allowed_egress_domains == ("*",)


# ---------------------------------------------------------------------------
# Resource limits always set
# ---------------------------------------------------------------------------


class TestResourceLimitsAlwaysSet:
    """Every profile must set memory and CPU limits."""

    @pytest.mark.parametrize(
        "factory",
        [
            SandboxSecurityProfile.strict,
            SandboxSecurityProfile.standard,
            SandboxSecurityProfile.permissive,
        ],
    )
    def test_memory_limit_set(self, factory) -> None:
        kwargs = factory().to_docker_kwargs()
        assert "mem_limit" in kwargs
        assert kwargs["mem_limit"]  # not empty

    @pytest.mark.parametrize(
        "factory",
        [
            SandboxSecurityProfile.strict,
            SandboxSecurityProfile.standard,
            SandboxSecurityProfile.permissive,
        ],
    )
    def test_cpu_limit_set(self, factory) -> None:
        kwargs = factory().to_docker_kwargs()
        assert kwargs["cpu_quota"] > 0
        assert kwargs["cpu_period"] == 100000

    def test_strict_has_tightest_limits(self) -> None:
        strict = SandboxSecurityProfile.strict()
        standard = SandboxSecurityProfile.standard()
        permissive = SandboxSecurityProfile.permissive()
        assert strict.cpu_limit < standard.cpu_limit < permissive.cpu_limit


# ---------------------------------------------------------------------------
# Security options always present
# ---------------------------------------------------------------------------


class TestSecurityOptsAlwaysPresent:
    """Every profile must have security_opt set."""

    @pytest.mark.parametrize(
        "factory",
        [
            SandboxSecurityProfile.strict,
            SandboxSecurityProfile.standard,
            SandboxSecurityProfile.permissive,
        ],
    )
    def test_security_opt_present(self, factory) -> None:
        kwargs = factory().to_docker_kwargs()
        assert "security_opt" in kwargs
        assert len(kwargs["security_opt"]) > 0

    @pytest.mark.parametrize(
        "factory",
        [
            SandboxSecurityProfile.strict,
            SandboxSecurityProfile.standard,
            SandboxSecurityProfile.permissive,
        ],
    )
    def test_no_new_privileges_set(self, factory) -> None:
        kwargs = factory().to_docker_kwargs()
        opts = kwargs["security_opt"]
        has_no_new_privs = any("no-new-privileges:" in opt for opt in opts)
        assert has_no_new_privs


# ---------------------------------------------------------------------------
# Trust tier completeness
# ---------------------------------------------------------------------------


class TestTrustTierCompleteness:
    """Trust tier mapping must cover all expected agent types."""

    def test_all_standard_agents_mapped(self) -> None:
        expected = {"UntrustedAgent", "SkillBuilder", "ImpersonatedUser"}
        assert set(TRUST_TIER_PROFILE_MAP.keys()) == expected

    def test_untrusted_gets_strictest(self) -> None:
        profile = SandboxSecurityProfile.from_trust_tier("UntrustedAgent")
        assert profile.name == "strict"
        assert profile.network_mode == "none"
        assert profile.allow_fuse is False

    def test_unknown_agents_get_standard_not_permissive(self) -> None:
        """Unknown agents must default to standard (not permissive)."""
        profile = SandboxSecurityProfile.from_trust_tier("UnknownAgent")
        assert profile.name == "standard"

    def test_none_agent_gets_standard_not_permissive(self) -> None:
        """None agent must default to standard (not permissive)."""
        profile = SandboxSecurityProfile.from_trust_tier(None)
        assert profile.name == "standard"


# ---------------------------------------------------------------------------
# Custom profile injection defense
# ---------------------------------------------------------------------------


class TestCustomProfileDefense:
    """Manually constructed profiles must still produce safe Docker kwargs."""

    def test_custom_profile_without_fuse_gets_no_new_privileges(self) -> None:
        profile = SandboxSecurityProfile(
            name="custom",
            network_mode="none",
            memory_limit="128m",
            cpu_limit=0.25,
            allow_fuse=False,
        )
        kwargs = profile.to_docker_kwargs()
        assert "no-new-privileges:true" in kwargs["security_opt"]
        assert "cap_add" not in kwargs

    def test_custom_profile_with_fuse_gets_sys_admin(self) -> None:
        profile = SandboxSecurityProfile(
            name="custom-fuse",
            network_mode="none",
            memory_limit="256m",
            cpu_limit=0.5,
            allow_fuse=True,
        )
        kwargs = profile.to_docker_kwargs()
        assert "no-new-privileges:false" in kwargs["security_opt"]
        assert "SYS_ADMIN" in kwargs["cap_add"]

    def test_custom_seccomp_in_security_opt(self) -> None:
        profile = SandboxSecurityProfile(
            name="custom-seccomp",
            network_mode="none",
            seccomp_profile="/path/to/seccomp.json",
            memory_limit="256m",
            cpu_limit=0.5,
        )
        kwargs = profile.to_docker_kwargs()
        assert "seccomp=/path/to/seccomp.json" in kwargs["security_opt"]


# ---------------------------------------------------------------------------
# Docker kwargs completeness — nothing dangerous leaks
# ---------------------------------------------------------------------------


class TestDockerKwargsCompleteness:
    """Verify Docker kwargs don't include dangerous settings."""

    @pytest.mark.parametrize(
        "factory",
        [
            SandboxSecurityProfile.strict,
            SandboxSecurityProfile.standard,
            SandboxSecurityProfile.permissive,
        ],
    )
    def test_no_privileged_mode(self, factory) -> None:
        kwargs = factory().to_docker_kwargs()
        assert kwargs.get("privileged") is not True

    @pytest.mark.parametrize(
        "factory",
        [
            SandboxSecurityProfile.strict,
            SandboxSecurityProfile.standard,
            SandboxSecurityProfile.permissive,
        ],
    )
    def test_no_pid_host(self, factory) -> None:
        kwargs = factory().to_docker_kwargs()
        assert kwargs.get("pid_mode") != "host"

    @pytest.mark.parametrize(
        "factory",
        [
            SandboxSecurityProfile.strict,
            SandboxSecurityProfile.standard,
            SandboxSecurityProfile.permissive,
        ],
    )
    def test_no_ipc_host(self, factory) -> None:
        kwargs = factory().to_docker_kwargs()
        assert kwargs.get("ipc_mode") != "host"

    @pytest.mark.parametrize(
        "factory",
        [
            SandboxSecurityProfile.strict,
            SandboxSecurityProfile.standard,
            SandboxSecurityProfile.permissive,
        ],
    )
    def test_pids_limit_set(self, factory) -> None:
        kwargs = factory().to_docker_kwargs()
        assert kwargs["pids_limit"] > 0

    def test_strict_has_lowest_pids_limit(self) -> None:
        strict = SandboxSecurityProfile.strict()
        standard = SandboxSecurityProfile.standard()
        permissive = SandboxSecurityProfile.permissive()
        assert strict.pids_limit <= standard.pids_limit <= permissive.pids_limit
