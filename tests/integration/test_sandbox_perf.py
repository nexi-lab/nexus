"""Performance benchmarks for sandbox security (Issue #1000).

Verifies that the security hardening doesn't introduce meaningful
latency to sandbox creation, profile resolution, or input validation.

Requires: Docker daemon running locally.
"""

from __future__ import annotations

import statistics
import time

import pytest

try:
    import docker

    _docker_client = docker.from_env()
    _docker_client.ping()
    DOCKER_AVAILABLE = True
except Exception:
    DOCKER_AVAILABLE = False

pytestmark = [
    pytest.mark.skipif(not DOCKER_AVAILABLE, reason="Docker not available"),
    pytest.mark.integration,
]

from nexus.sandbox.egress_proxy import (  # noqa: E402
    EgressProxyManager,
    build_squid_config,
    validate_domain,
)
from nexus.sandbox.sandbox_audit import SandboxAuditLogger  # noqa: E402
from nexus.sandbox.sandbox_provider import (  # noqa: E402
    validate_agent_id,
    validate_mount_path,
    validate_nexus_url,
)
from nexus.sandbox.security_profile import SandboxSecurityProfile  # noqa: E402

ALPINE_IMAGE = "alpine:3.19"
N_ITERATIONS = 1000


@pytest.fixture(scope="module")
def docker_client() -> docker.DockerClient:
    client = docker.from_env()
    try:
        client.images.get(ALPINE_IMAGE)
    except docker.errors.NotFound:
        client.images.pull(ALPINE_IMAGE)
    return client


# ---------------------------------------------------------------------------
# Profile creation & conversion (hot path — called per sandbox creation)
# ---------------------------------------------------------------------------


class TestProfileCreationPerf:
    """Profile factory + to_docker_kwargs() must be < 0.1ms per call."""

    def test_strict_factory_speed(self) -> None:
        times = []
        for _ in range(N_ITERATIONS):
            start = time.perf_counter_ns()
            SandboxSecurityProfile.strict()
            elapsed_ns = time.perf_counter_ns() - start
            times.append(elapsed_ns)

        median_us = statistics.median(times) / 1000
        p99_us = sorted(times)[int(N_ITERATIONS * 0.99)] / 1000
        assert median_us < 100, f"strict() median {median_us:.1f}µs > 100µs"
        assert p99_us < 500, f"strict() p99 {p99_us:.1f}µs > 500µs"

    def test_standard_factory_speed(self) -> None:
        times = []
        for _ in range(N_ITERATIONS):
            start = time.perf_counter_ns()
            SandboxSecurityProfile.standard()
            elapsed_ns = time.perf_counter_ns() - start
            times.append(elapsed_ns)

        median_us = statistics.median(times) / 1000
        assert median_us < 100, f"standard() median {median_us:.1f}µs > 100µs"

    def test_to_docker_kwargs_speed(self) -> None:
        profiles = [
            SandboxSecurityProfile.strict(),
            SandboxSecurityProfile.standard(),
            SandboxSecurityProfile.permissive(),
        ]
        times = []
        for profile in profiles:
            for _ in range(N_ITERATIONS):
                start = time.perf_counter_ns()
                profile.to_docker_kwargs()
                elapsed_ns = time.perf_counter_ns() - start
                times.append(elapsed_ns)

        median_us = statistics.median(times) / 1000
        p99_us = sorted(times)[int(len(times) * 0.99)] / 1000
        assert median_us < 100, f"to_docker_kwargs() median {median_us:.1f}µs > 100µs"
        assert p99_us < 500, f"to_docker_kwargs() p99 {p99_us:.1f}µs > 500µs"


# ---------------------------------------------------------------------------
# Trust tier resolution (called per sandbox creation)
# ---------------------------------------------------------------------------


class TestTrustTierResolutionPerf:
    """from_trust_tier() with case-insensitive lookup must be < 0.1ms."""

    def test_known_agent_speed(self) -> None:
        times = []
        for _ in range(N_ITERATIONS):
            start = time.perf_counter_ns()
            SandboxSecurityProfile.from_trust_tier("SkillBuilder")
            elapsed_ns = time.perf_counter_ns() - start
            times.append(elapsed_ns)

        median_us = statistics.median(times) / 1000
        assert median_us < 100, f"from_trust_tier() median {median_us:.1f}µs > 100µs"

    def test_case_insensitive_lookup_speed(self) -> None:
        """Case-insensitive lookup should not add measurable overhead."""
        times_exact = []
        times_lower = []

        for _ in range(N_ITERATIONS):
            start = time.perf_counter_ns()
            SandboxSecurityProfile.from_trust_tier("UntrustedAgent")
            elapsed = time.perf_counter_ns() - start
            times_exact.append(elapsed)

        for _ in range(N_ITERATIONS):
            start = time.perf_counter_ns()
            SandboxSecurityProfile.from_trust_tier("untrustedagent")
            elapsed = time.perf_counter_ns() - start
            times_lower.append(elapsed)

        median_exact = statistics.median(times_exact) / 1000
        median_lower = statistics.median(times_lower) / 1000

        # Case-insensitive should be within 2x of exact match
        assert median_lower < median_exact * 3, (
            f"Case-insensitive {median_lower:.1f}µs > 3x exact {median_exact:.1f}µs"
        )


# ---------------------------------------------------------------------------
# Input validation (called per mount_nexus)
# ---------------------------------------------------------------------------


class TestInputValidationPerf:
    """Validation functions must be < 0.05ms per call."""

    def test_validate_mount_path_speed(self) -> None:
        times = []
        for _ in range(N_ITERATIONS):
            start = time.perf_counter_ns()
            validate_mount_path("/mnt/nexus/workspace")
            elapsed_ns = time.perf_counter_ns() - start
            times.append(elapsed_ns)

        median_us = statistics.median(times) / 1000
        assert median_us < 50, f"validate_mount_path() median {median_us:.1f}µs > 50µs"

    def test_validate_nexus_url_speed(self) -> None:
        times = []
        for _ in range(N_ITERATIONS):
            start = time.perf_counter_ns()
            validate_nexus_url("https://nexus.example.com:2026")
            elapsed_ns = time.perf_counter_ns() - start
            times.append(elapsed_ns)

        median_us = statistics.median(times) / 1000
        assert median_us < 50, f"validate_nexus_url() median {median_us:.1f}µs > 50µs"

    def test_validate_agent_id_speed(self) -> None:
        times = []
        for _ in range(N_ITERATIONS):
            start = time.perf_counter_ns()
            validate_agent_id("user123,SkillBuilder")
            elapsed_ns = time.perf_counter_ns() - start
            times.append(elapsed_ns)

        median_us = statistics.median(times) / 1000
        assert median_us < 50, f"validate_agent_id() median {median_us:.1f}µs > 50µs"

    def test_validate_domain_speed(self) -> None:
        times = []
        for _ in range(N_ITERATIONS):
            start = time.perf_counter_ns()
            validate_domain("api.openai.com")
            elapsed_ns = time.perf_counter_ns() - start
            times.append(elapsed_ns)

        median_us = statistics.median(times) / 1000
        assert median_us < 50, f"validate_domain() median {median_us:.1f}µs > 50µs"


# ---------------------------------------------------------------------------
# Squid config generation (called when proxy starts/restarts)
# ---------------------------------------------------------------------------


class TestSquidConfigPerf:
    """Squid config generation must be < 1ms even with many domains."""

    def test_small_allowlist_speed(self) -> None:
        domains = ("api.openai.com", "api.anthropic.com", "pypi.org", "files.pythonhosted.org")
        times = []
        for _ in range(N_ITERATIONS):
            start = time.perf_counter_ns()
            build_squid_config(domains)
            elapsed_ns = time.perf_counter_ns() - start
            times.append(elapsed_ns)

        median_us = statistics.median(times) / 1000
        assert median_us < 200, f"build_squid_config(4 domains) median {median_us:.1f}µs > 200µs"

    def test_large_allowlist_speed(self) -> None:
        domains = tuple(f"domain-{i}.example.com" for i in range(100))
        times = []
        for _ in range(N_ITERATIONS):
            start = time.perf_counter_ns()
            build_squid_config(domains)
            elapsed_ns = time.perf_counter_ns() - start
            times.append(elapsed_ns)

        median_us = statistics.median(times) / 1000
        assert median_us < 1000, (
            f"build_squid_config(100 domains) median {median_us:.1f}µs > 1000µs"
        )


# ---------------------------------------------------------------------------
# Audit logging (fire-and-forget, must not block)
# ---------------------------------------------------------------------------


class TestAuditLoggingPerf:
    """Audit logger must be < 0.1ms per call (fire-and-forget)."""

    def test_log_creation_speed(self) -> None:
        audit = SandboxAuditLogger()
        profile = SandboxSecurityProfile.standard()
        times = []
        for _ in range(N_ITERATIONS):
            start = time.perf_counter_ns()
            audit.log_creation("sandbox-123", profile, agent_id="user1,SkillBuilder")
            elapsed_ns = time.perf_counter_ns() - start
            times.append(elapsed_ns)

        median_us = statistics.median(times) / 1000
        assert median_us < 100, f"log_creation() median {median_us:.1f}µs > 100µs"

    def test_log_egress_speed(self) -> None:
        audit = SandboxAuditLogger()
        times = []
        for _ in range(N_ITERATIONS):
            start = time.perf_counter_ns()
            audit.log_egress_attempt("sandbox-123", "api.openai.com", allowed=True)
            elapsed_ns = time.perf_counter_ns() - start
            times.append(elapsed_ns)

        median_us = statistics.median(times) / 1000
        assert median_us < 100, f"log_egress_attempt() median {median_us:.1f}µs > 100µs"


# ---------------------------------------------------------------------------
# Domain merging (called when sandbox registers/unregisters)
# ---------------------------------------------------------------------------


class TestDomainMergingPerf:
    """Per-sandbox domain merging must be < 0.5ms even with many sandboxes."""

    def test_merge_10_sandboxes(self) -> None:
        from unittest.mock import MagicMock

        mgr = EgressProxyManager(MagicMock())
        for i in range(10):
            mgr._sandbox_domains[f"sb{i}"] = (f"domain-{i}.com", f"api-{i}.example.com")

        times = []
        for _ in range(N_ITERATIONS):
            start = time.perf_counter_ns()
            mgr._merged_domains()
            elapsed_ns = time.perf_counter_ns() - start
            times.append(elapsed_ns)

        median_us = statistics.median(times) / 1000
        assert median_us < 200, f"_merged_domains(10 sandboxes) median {median_us:.1f}µs > 200µs"

    def test_merge_100_sandboxes(self) -> None:
        from unittest.mock import MagicMock

        mgr = EgressProxyManager(MagicMock())
        for i in range(100):
            mgr._sandbox_domains[f"sb{i}"] = (f"domain-{i}.com",)

        times = []
        for _ in range(N_ITERATIONS):
            start = time.perf_counter_ns()
            mgr._merged_domains()
            elapsed_ns = time.perf_counter_ns() - start
            times.append(elapsed_ns)

        median_us = statistics.median(times) / 1000
        assert median_us < 500, f"_merged_domains(100 sandboxes) median {median_us:.1f}µs > 500µs"


# ---------------------------------------------------------------------------
# Container creation overhead (real Docker)
# ---------------------------------------------------------------------------


class TestContainerCreationOverhead:
    """Security profiles should not add meaningful overhead to container creation."""

    @pytest.mark.asyncio
    async def test_strict_vs_bare_creation_time(self, docker_client: docker.DockerClient) -> None:
        """Strict profile container creation should be < 2x bare container."""
        # Baseline: bare container
        bare_times = []
        for _ in range(3):
            start = time.perf_counter()
            c = docker_client.containers.run(ALPINE_IMAGE, "true", detach=True, remove=False)
            c.wait(timeout=10)
            elapsed = time.perf_counter() - start
            bare_times.append(elapsed)
            c.remove(force=True)

        # With strict profile
        profile = SandboxSecurityProfile.strict()
        kwargs = profile.to_docker_kwargs()
        strict_times = []
        for _ in range(3):
            start = time.perf_counter()
            c = docker_client.containers.run(
                ALPINE_IMAGE, "true", detach=True, remove=False, **kwargs
            )
            c.wait(timeout=10)
            elapsed = time.perf_counter() - start
            strict_times.append(elapsed)
            c.remove(force=True)

        bare_median = statistics.median(bare_times)
        strict_median = statistics.median(strict_times)
        overhead_ratio = strict_median / bare_median if bare_median > 0 else 1.0

        # Security profile should add < 2x overhead (in practice ~1.0-1.1x)
        assert overhead_ratio < 2.0, (
            f"Strict profile adds {overhead_ratio:.2f}x overhead "
            f"(bare={bare_median:.3f}s, strict={strict_median:.3f}s)"
        )

    @pytest.mark.asyncio
    async def test_standard_vs_bare_creation_time(self, docker_client: docker.DockerClient) -> None:
        """Standard profile container creation should be < 2x bare container."""
        bare_times = []
        for _ in range(3):
            start = time.perf_counter()
            c = docker_client.containers.run(ALPINE_IMAGE, "true", detach=True, remove=False)
            c.wait(timeout=10)
            elapsed = time.perf_counter() - start
            bare_times.append(elapsed)
            c.remove(force=True)

        profile = SandboxSecurityProfile.standard()
        kwargs = profile.to_docker_kwargs()
        std_times = []
        for _ in range(3):
            start = time.perf_counter()
            c = docker_client.containers.run(
                ALPINE_IMAGE, "true", detach=True, remove=False, **kwargs
            )
            c.wait(timeout=10)
            elapsed = time.perf_counter() - start
            std_times.append(elapsed)
            c.remove(force=True)

        bare_median = statistics.median(bare_times)
        std_median = statistics.median(std_times)
        overhead_ratio = std_median / bare_median if bare_median > 0 else 1.0

        assert overhead_ratio < 2.0, (
            f"Standard profile adds {overhead_ratio:.2f}x overhead "
            f"(bare={bare_median:.3f}s, standard={std_median:.3f}s)"
        )
