"""Integration tests for sandbox security profiles (Issue #1000).

These tests create real Docker containers with security profiles applied
and verify that the isolation constraints are actually enforced.

Requires:
    - Docker daemon running locally
    - ``docker`` Python package installed

Skip with: pytest -m "not docker"
"""

from __future__ import annotations

import logging

import pytest

logger = logging.getLogger(__name__)

# Skip entire module if docker is not available
try:
    import docker
    from docker.errors import NotFound

    _docker_client = docker.from_env()
    _docker_client.ping()
    DOCKER_AVAILABLE = True
except Exception:
    DOCKER_AVAILABLE = False

pytestmark = [
    pytest.mark.skipif(not DOCKER_AVAILABLE, reason="Docker not available"),
    pytest.mark.integration,
]


from nexus.sandbox.security_profile import SandboxSecurityProfile  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ALPINE_IMAGE = "alpine:3.19"


async def _run_in_container(
    client: docker.DockerClient,
    profile: SandboxSecurityProfile,
    command: str,
    timeout: int = 10,
) -> tuple[int, str]:
    """Create a container with the given profile, run a command, return (exit_code, output)."""
    kwargs = profile.to_docker_kwargs()

    # Remove network_mode=none for tests that need DNS — use host network
    # for tests that specifically test network, keep network_mode as-is
    container = None
    try:
        container = client.containers.run(
            image=ALPINE_IMAGE,
            command=["sh", "-c", command],
            detach=True,
            remove=False,
            **kwargs,
        )
        # Wait for container to finish
        result = container.wait(timeout=timeout)
        exit_code = result.get("StatusCode", -1)
        output = container.logs().decode("utf-8", errors="replace")
        return exit_code, output
    finally:
        if container:
            try:
                container.remove(force=True)
            except Exception:
                pass


@pytest.fixture(scope="module")
def docker_client() -> docker.DockerClient:
    """Docker client for integration tests."""
    client = docker.from_env()
    # Ensure Alpine image is available
    try:
        client.images.get(ALPINE_IMAGE)
    except NotFound:
        logger.info("Pulling %s for integration tests...", ALPINE_IMAGE)
        client.images.pull(ALPINE_IMAGE)
    return client


# ---------------------------------------------------------------------------
# Strict profile: maximum lockdown
# ---------------------------------------------------------------------------


class TestStrictProfileEnforcement:
    """Verify strict profile constraints are enforced in real containers."""

    @pytest.fixture()
    def profile(self) -> SandboxSecurityProfile:
        return SandboxSecurityProfile.strict()

    @pytest.mark.asyncio
    async def test_network_is_disabled(
        self, docker_client: docker.DockerClient, profile: SandboxSecurityProfile
    ) -> None:
        """Strict container cannot reach the internet."""
        exit_code, output = await _run_in_container(
            docker_client,
            profile,
            "wget -T 3 -q http://1.1.1.1 -O /dev/null 2>&1 || echo 'NETWORK_BLOCKED'",
        )
        assert "NETWORK_BLOCKED" in output

    @pytest.mark.asyncio
    async def test_root_filesystem_is_readonly(
        self, docker_client: docker.DockerClient, profile: SandboxSecurityProfile
    ) -> None:
        """Strict container has read-only root filesystem."""
        exit_code, output = await _run_in_container(
            docker_client,
            profile,
            "touch /testfile 2>&1 || echo 'READONLY'",
        )
        assert "READONLY" in output or "Read-only" in output

    @pytest.mark.asyncio
    async def test_tmp_is_writable(
        self, docker_client: docker.DockerClient, profile: SandboxSecurityProfile
    ) -> None:
        """Strict container can write to /tmp (tmpfs mount)."""
        exit_code, output = await _run_in_container(
            docker_client,
            profile,
            "echo 'hello' > /tmp/test.txt && cat /tmp/test.txt",
        )
        assert exit_code == 0
        assert "hello" in output

    @pytest.mark.asyncio
    async def test_capabilities_dropped(
        self, docker_client: docker.DockerClient, profile: SandboxSecurityProfile
    ) -> None:
        """Strict container has all capabilities dropped."""
        # Try to use chown (requires CAP_CHOWN which should be dropped)
        exit_code, output = await _run_in_container(
            docker_client,
            profile,
            "adduser -D testuser 2>&1 && chown testuser /tmp 2>&1 || echo 'CAP_DROPPED'",
        )
        assert "CAP_DROPPED" in output or exit_code != 0


# ---------------------------------------------------------------------------
# Standard profile: balanced isolation
# ---------------------------------------------------------------------------


class TestStandardProfileEnforcement:
    """Verify standard profile constraints in real containers."""

    @pytest.fixture()
    def profile(self) -> SandboxSecurityProfile:
        return SandboxSecurityProfile.standard()

    @pytest.mark.asyncio
    async def test_network_is_disabled(
        self, docker_client: docker.DockerClient, profile: SandboxSecurityProfile
    ) -> None:
        """Standard container cannot reach the internet (network=none)."""
        exit_code, output = await _run_in_container(
            docker_client,
            profile,
            "wget -T 3 -q http://1.1.1.1 -O /dev/null 2>&1 || echo 'NETWORK_BLOCKED'",
        )
        assert "NETWORK_BLOCKED" in output

    @pytest.mark.asyncio
    async def test_root_filesystem_is_readonly(
        self, docker_client: docker.DockerClient, profile: SandboxSecurityProfile
    ) -> None:
        """Standard container has read-only root filesystem."""
        exit_code, output = await _run_in_container(
            docker_client,
            profile,
            "touch /testfile 2>&1 || echo 'READONLY'",
        )
        assert "READONLY" in output or "Read-only" in output

    @pytest.mark.asyncio
    async def test_all_caps_dropped_except_sys_admin(
        self, docker_client: docker.DockerClient, profile: SandboxSecurityProfile
    ) -> None:
        """Standard drops ALL caps, adds back only SYS_ADMIN for FUSE."""
        kwargs = profile.to_docker_kwargs()
        assert kwargs["cap_drop"] == ["ALL"]
        assert "SYS_ADMIN" in kwargs.get("cap_add", [])


# ---------------------------------------------------------------------------
# Permissive profile: network access works
# ---------------------------------------------------------------------------


class TestPermissiveProfileEnforcement:
    """Verify permissive profile has expected access."""

    @pytest.fixture()
    def profile(self) -> SandboxSecurityProfile:
        return SandboxSecurityProfile.permissive()

    @pytest.mark.asyncio
    async def test_network_is_available(
        self, docker_client: docker.DockerClient, profile: SandboxSecurityProfile
    ) -> None:
        """Permissive container CAN reach the internet (bridge network)."""
        exit_code, output = await _run_in_container(
            docker_client,
            profile,
            "wget -T 5 -q http://1.1.1.1 -O /dev/null 2>&1 && echo 'NETWORK_OK' || echo 'NETWORK_FAIL'",
        )
        # Permissive uses default bridge, should have network access
        assert "NETWORK_OK" in output

    @pytest.mark.asyncio
    async def test_root_filesystem_is_writable(
        self, docker_client: docker.DockerClient, profile: SandboxSecurityProfile
    ) -> None:
        """Permissive container has writable root filesystem."""
        exit_code, output = await _run_in_container(
            docker_client,
            profile,
            "touch /testfile && echo 'WRITABLE'",
        )
        assert exit_code == 0
        assert "WRITABLE" in output


# ---------------------------------------------------------------------------
# Docker inspect verification — security kwargs are applied
# ---------------------------------------------------------------------------


class TestDockerInspectVerification:
    """Verify Docker inspect confirms security settings are applied."""

    @pytest.mark.asyncio
    async def test_strict_inspect_shows_correct_settings(
        self, docker_client: docker.DockerClient
    ) -> None:
        """Verify docker inspect confirms strict security settings."""
        profile = SandboxSecurityProfile.strict()
        kwargs = profile.to_docker_kwargs()

        container = docker_client.containers.run(
            image=ALPINE_IMAGE,
            command=["sleep", "5"],
            detach=True,
            **kwargs,
        )
        try:
            info = docker_client.api.inspect_container(container.id)
            host_config = info["HostConfig"]

            # Network mode
            assert host_config["NetworkMode"] == "none"

            # Capabilities
            assert host_config["CapDrop"] == ["ALL"]
            assert host_config["CapAdd"] is None or host_config["CapAdd"] == []

            # Read-only root
            assert host_config["ReadonlyRootfs"] is True

            # Memory limit (256m = 268435456 bytes)
            assert host_config["Memory"] == 256 * 1024 * 1024

            # PID limit
            assert host_config["PidsLimit"] == 128

            # Security options should include no-new-privileges
            sec_opts = host_config.get("SecurityOpt", [])
            assert "no-new-privileges:true" in sec_opts

            # Should NOT have apparmor=unconfined
            for opt in sec_opts:
                assert "apparmor=unconfined" not in opt

        finally:
            container.stop(timeout=1)
            container.remove(force=True)

    @pytest.mark.asyncio
    async def test_standard_inspect_shows_sys_admin_only(
        self, docker_client: docker.DockerClient
    ) -> None:
        """Verify standard profile drops ALL caps and adds back only SYS_ADMIN."""
        profile = SandboxSecurityProfile.standard()
        kwargs = profile.to_docker_kwargs()

        container = docker_client.containers.run(
            image=ALPINE_IMAGE,
            command=["sleep", "5"],
            detach=True,
            **kwargs,
        )
        try:
            info = docker_client.api.inspect_container(container.id)
            host_config = info["HostConfig"]

            # Drop ALL
            assert host_config["CapDrop"] == ["ALL"]

            # Add back only SYS_ADMIN (for FUSE)
            assert host_config["CapAdd"] == ["SYS_ADMIN"]

            # Memory limit (512m)
            assert host_config["Memory"] == 512 * 1024 * 1024

            # PID limit
            assert host_config["PidsLimit"] == 256

            # no-new-privileges:false (needed for FUSE)
            sec_opts = host_config.get("SecurityOpt", [])
            assert "no-new-privileges:false" in sec_opts

        finally:
            container.stop(timeout=1)
            container.remove(force=True)


# ---------------------------------------------------------------------------
# Resource limit enforcement
# ---------------------------------------------------------------------------


class TestResourceLimitEnforcement:
    """Verify resource limits are actually enforced."""

    @pytest.mark.asyncio
    async def test_memory_limit_enforced(self, docker_client: docker.DockerClient) -> None:
        """Strict container cannot allocate more than 256MB."""
        profile = SandboxSecurityProfile.strict()
        # Try to allocate 512MB in a 256MB container — should fail or be killed
        exit_code, output = await _run_in_container(
            docker_client,
            profile,
            # Use dd to try allocating 300MB (more than 256MB limit)
            "dd if=/dev/zero of=/tmp/bigfile bs=1M count=300 2>&1 || echo 'MEM_LIMITED'",
            timeout=15,
        )
        # Container should either be killed (exit 137) or dd should fail
        assert exit_code != 0 or "MEM_LIMITED" in output or "Killed" in output

    @pytest.mark.asyncio
    async def test_pids_limit_enforced(self, docker_client: docker.DockerClient) -> None:
        """Strict container cannot fork more than pids_limit processes."""
        profile = SandboxSecurityProfile.strict()
        # Try to create many processes (more than 128 pids_limit)
        exit_code, output = await _run_in_container(
            docker_client,
            profile,
            "for i in $(seq 1 200); do sleep 60 & done 2>&1; echo EXIT=$?",
            timeout=15,
        )
        # Should hit resource limit — some forks should fail
        assert "Resource" in output or "Cannot" in output or exit_code != 0 or "EXIT=1" in output


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    """Verify input validation prevents command injection."""

    def test_mount_path_rejects_injection(self) -> None:
        from nexus.sandbox.sandbox_provider import validate_mount_path

        with pytest.raises(ValueError):
            validate_mount_path("/mnt/nexus; rm -rf /")

        with pytest.raises(ValueError):
            validate_mount_path("/mnt/nexus$(whoami)")

        with pytest.raises(ValueError):
            validate_mount_path("relative/path")

        # Valid paths
        assert validate_mount_path("/mnt/nexus") == "/mnt/nexus"
        assert validate_mount_path("/home/user/data") == "/home/user/data"

    def test_nexus_url_rejects_injection(self) -> None:
        from nexus.sandbox.sandbox_provider import validate_nexus_url

        with pytest.raises(ValueError):
            validate_nexus_url("http://localhost; rm -rf /")

        with pytest.raises(ValueError):
            validate_nexus_url("ftp://evil.com")

        with pytest.raises(ValueError):
            validate_nexus_url("http://localhost$(whoami)")

        # Valid URLs
        assert validate_nexus_url("http://localhost:2026") == "http://localhost:2026"
        assert validate_nexus_url("https://nexus.example.com") == "https://nexus.example.com"

    def test_agent_id_rejects_injection(self) -> None:
        from nexus.sandbox.sandbox_provider import validate_agent_id

        with pytest.raises(ValueError):
            validate_agent_id("agent; rm -rf /")

        with pytest.raises(ValueError):
            validate_agent_id("agent$(whoami)")

        # Valid agent IDs
        assert validate_agent_id("user1,SkillBuilder") == "user1,SkillBuilder"
        assert validate_agent_id("agent-123") == "agent-123"

    def test_egress_domain_rejects_injection(self) -> None:
        from nexus.sandbox.egress_proxy import validate_domain

        with pytest.raises(ValueError):
            validate_domain("evil.com'; rm -rf /")

        with pytest.raises(ValueError):
            validate_domain("$(whoami).evil.com")

        # Valid domains
        assert validate_domain("api.openai.com") == "api.openai.com"
        assert validate_domain("*") == "*"


# ---------------------------------------------------------------------------
# Trust tier mapping
# ---------------------------------------------------------------------------


class TestTrustTierIntegration:
    """Verify trust tier → profile mapping end-to-end."""

    def test_case_insensitive_prevents_bypass(self) -> None:
        """Attackers cannot bypass strict profile by changing case."""
        strict_profile = SandboxSecurityProfile.from_trust_tier("UntrustedAgent")
        # Try different casings — all should resolve to strict
        for variant in ["untrustedagent", "UNTRUSTEDAGENT", "UntrustedAgent", "untrustedAgent"]:
            profile = SandboxSecurityProfile.from_trust_tier(variant)
            assert profile.name == strict_profile.name, (
                f"Case variant '{variant}' resolved to '{profile.name}' instead of 'strict'"
            )

    def test_unknown_agent_gets_standard_not_permissive(self) -> None:
        """Unknown agents must default to standard, never permissive."""
        for agent in ["RandomAgent", "attacker", "admin", ""]:
            if agent:  # empty string is handled differently
                profile = SandboxSecurityProfile.from_trust_tier(agent)
                assert profile.name == "standard", (
                    f"Unknown agent '{agent}' got '{profile.name}' instead of 'standard'"
                )
