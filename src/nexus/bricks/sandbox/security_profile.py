"""Security profiles for sandbox containers.

Defines immutable security configurations that map agent trust tiers
to concrete Docker container settings (capabilities, network, filesystem, resources).

Issue #1000: Enhance agent sandboxing with network isolation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Default egress allowlist for standard profile
DEFAULT_EGRESS_ALLOWLIST = (
    "api.openai.com",
    "api.anthropic.com",
    "pypi.org",
    "files.pythonhosted.org",
)

# Trust tier to profile name mapping
TRUST_TIER_PROFILE_MAP: dict[str, str] = {
    "UntrustedAgent": "strict",
    "SkillBuilder": "standard",
    "ImpersonatedUser": "permissive",
}


@dataclass(frozen=True)
class SandboxSecurityProfile:
    """Immutable security configuration for sandbox containers.

    Each profile encapsulates all security-relevant Docker container settings.
    Use factory methods to get pre-built profiles for each trust tier.

    Attributes:
        name: Profile identifier ("strict", "standard", "permissive").
        network_mode: Docker network mode ("none", or None for default bridge).
        capabilities_add: Linux capabilities to add (empty = none).
        capabilities_drop: Linux capabilities to drop.
        seccomp_profile: Path to custom seccomp JSON, or None for Docker default.
        read_only_root: Whether root filesystem is read-only.
        tmpfs_mounts: Tmpfs mount specs as tuples of (path, options).
        memory_limit: Container memory limit (e.g., "256m", "512m", "1g").
        cpu_limit: CPU limit in cores (e.g., 0.5, 1.0, 2.0).
        env_vars: Extra environment variables as tuples of (key, value).
        allowed_egress_domains: Domains the egress proxy should allow.
        allow_fuse: Whether FUSE mounts are permitted. When True, adds
            CAP_SYS_ADMIN and sets no-new-privileges:false (required for
            FUSE mount syscall and sudo inside the container). When False,
            no-new-privileges:true is set for maximum lockdown.

    Note:
        tmpfs_mounts and env_vars use tuple-of-tuples instead of dict
        so the dataclass is fully hashable (all fields immutable).

    Security note on FUSE:
        Docker FUSE mounts require CAP_SYS_ADMIN for the mount() syscall
        and no-new-privileges:false for sudo/fusermount inside containers.
        The strict profile disables FUSE entirely (no CAP_SYS_ADMIN,
        no-new-privileges:true). Standard/permissive profiles allow FUSE
        but compensate with network isolation, read-only root, and Docker's
        default AppArmor (no longer disabled with apparmor=unconfined).
    """

    name: str
    network_mode: str | None
    capabilities_add: tuple[str, ...] = ()
    capabilities_drop: tuple[str, ...] = ()
    seccomp_profile: str | None = None
    read_only_root: bool = False
    tmpfs_mounts: tuple[tuple[str, str], ...] = ()
    memory_limit: str = "512m"
    cpu_limit: float = 1.0
    env_vars: tuple[tuple[str, str], ...] = ()
    allowed_egress_domains: tuple[str, ...] = ()
    allow_fuse: bool = False
    pids_limit: int = 256

    @classmethod
    def strict(cls) -> SandboxSecurityProfile:
        """Maximum isolation for untrusted agents.

        - No network access (network=none)
        - All capabilities dropped, no FUSE mount allowed
        - no-new-privileges:true (maximum lockdown)
        - Read-only root filesystem with small tmpfs /tmp
        - Tight resource limits (256MB, 0.5 CPU)
        - No egress allowed
        """
        return cls(
            name="strict",
            network_mode="none",
            capabilities_add=(),
            capabilities_drop=("ALL",),
            seccomp_profile=None,  # Docker default seccomp
            read_only_root=True,
            tmpfs_mounts=(("/tmp", "size=10m,noexec"),),
            memory_limit="256m",
            cpu_limit=0.5,
            env_vars=(("PYTHONDONTWRITEBYTECODE", "1"),),
            allowed_egress_domains=(),
            allow_fuse=False,
            pids_limit=128,
        )

    @classmethod
    def standard(cls) -> SandboxSecurityProfile:
        """Balanced isolation for skill-builder and default agents.

        - No direct network (network=none), egress via proxy
        - FUSE allowed (adds SYS_ADMIN, requires no-new-privileges:false)
        - Drops ALL capabilities, then adds back only SYS_ADMIN (via allow_fuse)
        - Docker's default AppArmor enabled (NOT unconfined)
        - Read-only root filesystem with larger tmpfs /tmp
        - Moderate resource limits (512MB, 1 CPU)
        - Proxy allows LLM API and PyPI egress
        """
        return cls(
            name="standard",
            network_mode="none",
            capabilities_add=(),
            capabilities_drop=("ALL",),
            seccomp_profile=None,  # Docker default seccomp
            read_only_root=True,
            tmpfs_mounts=(("/tmp", "size=50m"),),
            memory_limit="512m",
            cpu_limit=1.0,
            env_vars=(("PYTHONDONTWRITEBYTECODE", "1"),),
            allowed_egress_domains=DEFAULT_EGRESS_ALLOWLIST,
            allow_fuse=True,
            pids_limit=256,
        )

    @classmethod
    def permissive(cls) -> SandboxSecurityProfile:
        """Minimal restrictions for trusted (impersonated user) agents.

        - Default bridge network (full network access)
        - FUSE allowed (adds SYS_ADMIN, requires no-new-privileges:false)
        - Drops ALL capabilities, then adds back only SYS_ADMIN (via allow_fuse)
        - Docker's default AppArmor enabled (NOT unconfined)
        - Writable root filesystem
        - Generous resource limits (1GB, 2 CPU)
        - All egress allowed
        """
        return cls(
            name="permissive",
            network_mode=None,  # Docker default bridge
            capabilities_add=(),
            capabilities_drop=("ALL",),
            seccomp_profile=None,  # Docker default seccomp
            read_only_root=False,
            tmpfs_mounts=(),
            memory_limit="1g",
            cpu_limit=2.0,
            env_vars=(),
            allowed_egress_domains=("*",),
            allow_fuse=True,
            pids_limit=512,
        )

    @classmethod
    def from_trust_tier(cls, agent_name: str | None) -> SandboxSecurityProfile:
        """Resolve agent trust tier to security profile.

        Args:
            agent_name: Agent name from agent_id (e.g., "UntrustedAgent",
                        "SkillBuilder", "ImpersonatedUser"). If None or
                        unrecognized, defaults to standard.

        Returns:
            Matching security profile.
        """
        if agent_name is None:
            logger.debug("No agent name provided, using standard profile")
            return cls.standard()

        # Case-insensitive lookup to prevent bypass via casing differences
        normalized = {k.lower(): v for k, v in TRUST_TIER_PROFILE_MAP.items()}
        profile_name = normalized.get(agent_name.lower())
        if profile_name is None:
            logger.debug(
                "Unrecognized agent name '%s', using standard profile",
                agent_name,
            )
            return cls.standard()

        factory = {
            "strict": cls.strict,
            "standard": cls.standard,
            "permissive": cls.permissive,
        }
        profile = factory[profile_name]()
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Resolved agent '%s' to '%s' security profile",
                agent_name,
                profile.name,
            )
        return profile

    def to_docker_kwargs(self) -> dict[str, Any]:
        """Convert profile to Docker SDK containers.run() keyword arguments.

        Returns:
            Dict of kwargs suitable for docker.containers.run().
            Does NOT include image, detach, name, or volumes — those are
            handled by the caller.
        """
        kwargs: dict[str, Any] = {}

        # Network
        if self.network_mode is not None:
            kwargs["network_mode"] = self.network_mode

        # Capabilities — FUSE mounts require SYS_ADMIN for mount() syscall
        cap_add = list(self.capabilities_add)
        if self.allow_fuse and "SYS_ADMIN" not in cap_add:
            cap_add.append("SYS_ADMIN")
        if cap_add:
            kwargs["cap_add"] = cap_add
        if self.capabilities_drop:
            kwargs["cap_drop"] = list(self.capabilities_drop)

        # Security options — Docker's default AppArmor is always enabled
        # (we never set apparmor=unconfined, which was the old insecure behavior)
        # FUSE needs no-new-privileges:false for sudo/fusermount inside container
        no_new_privs = "false" if self.allow_fuse else "true"
        security_opts = [f"no-new-privileges:{no_new_privs}"]
        if self.seccomp_profile:
            security_opts.append(f"seccomp={self.seccomp_profile}")
        kwargs["security_opt"] = security_opts

        # Filesystem
        if self.read_only_root:
            kwargs["read_only"] = True
        if self.tmpfs_mounts:
            kwargs["tmpfs"] = dict(self.tmpfs_mounts)

        # Resource limits
        kwargs["mem_limit"] = self.memory_limit
        kwargs["cpu_quota"] = int(self.cpu_limit * 100000)
        kwargs["cpu_period"] = 100000
        kwargs["pids_limit"] = self.pids_limit

        # Environment variables
        if self.env_vars:
            kwargs["environment"] = dict(self.env_vars)

        return kwargs
