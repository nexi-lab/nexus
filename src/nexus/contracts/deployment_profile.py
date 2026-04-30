"""Deployment profiles for Nexus feature gating.

Issue #1389: Feature flags for deployment modes (full/lite/embedded).
Issue #2194: Minimal boot mode for minimal deployments.
Issue #844:  REMOTE profile for client-side deployment (RemoteBackend proxy).

Each DeploymentProfile defines a default set of enabled bricks.
Individual brick overrides are supported via FeaturesConfig or env vars.
The profile sets the *defaults*; explicit overrides always win.

Lego Architecture reference: Part 10 — Edge Deployment.

Profile hierarchy (superset relationship):
    embedded ⊂ lite ⊂ sandbox ⊂ full ⊆ cloud

CLUSTER and REMOTE are orthogonal tiers — not part of the superset chain:
    cluster  (minimal multi-node — Raft + federation only; disjoint from embedded)
    remote   (no local bricks — NFS-client model; Issue #844)
"""

import logging
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.lib.performance_tuning import ProfileTuning

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Brick name constants — canonical names used across the system
# ---------------------------------------------------------------------------

# Services (gated by profile)
BRICK_EVENTLOG = "eventlog"
BRICK_NAMESPACE = "namespace"
BRICK_PERMISSIONS = "permissions"
BRICK_SCHEDULER = "scheduler"

# Infrastructure bricks
BRICK_CACHE = "cache"
BRICK_IPC = "ipc"
BRICK_OBSERVABILITY = "observability"
BRICK_UPLOADS = "uploads"
BRICK_RESILIENCY = "resiliency"

# Feature bricks
BRICK_SEARCH = "search"
BRICK_PAY = "pay"
BRICK_LLM = "llm"
BRICK_SANDBOX = "sandbox"
BRICK_WORKFLOWS = "workflows"
BRICK_DISCOVERY = "discovery"
BRICK_MCP = "mcp"
BRICK_MEMORY = "memory"
BRICK_SKILLS = "skills"
BRICK_ACCESS_MANIFEST = "access_manifest"
BRICK_CATALOG = "catalog"
BRICK_DELEGATION = "delegation"
BRICK_IDENTITY = "identity"
BRICK_SHARE_LINK = "share_link"
BRICK_VERSIONING = "versioning"
BRICK_WORKSPACE = "workspace"
BRICK_PORTABILITY = "portability"
BRICK_PARSERS = "parsers"
BRICK_SNAPSHOT = "snapshot"
BRICK_TASK_MANAGER = "task_manager"

BRICK_FEDERATION = "federation"

# ---------------------------------------------------------------------------
# Driver constants — `BackendFactory.build` matches `backend_type` strings
# ---------------------------------------------------------------------------
#
# Drivers are kernel-side dynamic backends instantiated at
# `sys_setattr(DT_MOUNT)` time.  Profile gating limits which `backend_type`
# strings the active deployment will accept; mounts requesting a disabled
# driver fail with a clear error instead of silently falling through to the
# kernel-default local-root branch.
#
# `local`/`path_local`/`local_connector`/`cas-local` are kernel defaults
# available in every profile and skip the gate (see
# `rust/kernel/src/hal/backend_factory.rs::is_driver_enabled`).

# Storage drivers (CAS / cloud / remote)
DRIVER_LOCAL = "local"
DRIVER_S3 = "s3"
DRIVER_GCS = "gcs"
DRIVER_GDRIVE = "gdrive"
DRIVER_REMOTE = "remote"

# AI / LLM connectors
DRIVER_OPENAI = "openai"
DRIVER_ANTHROPIC = "anthropic"

# Communication / messaging connectors
DRIVER_GMAIL = "gmail"
DRIVER_SLACK = "slack"
DRIVER_X = "x"
DRIVER_HN = "hn"
DRIVER_NOSTR = "nostr"

# Generic CLI proxy
DRIVER_CLI = "cli"

ALL_DRIVER_NAMES: frozenset[str] = frozenset(
    {
        DRIVER_LOCAL,
        DRIVER_S3,
        DRIVER_GCS,
        DRIVER_GDRIVE,
        DRIVER_REMOTE,
        DRIVER_OPENAI,
        DRIVER_ANTHROPIC,
        DRIVER_GMAIL,
        DRIVER_SLACK,
        DRIVER_X,
        DRIVER_HN,
        DRIVER_NOSTR,
        DRIVER_CLI,
    }
)

# All brick names for validation
ALL_BRICK_NAMES: frozenset[str] = frozenset(
    {
        BRICK_EVENTLOG,
        BRICK_NAMESPACE,
        BRICK_PERMISSIONS,
        BRICK_SCHEDULER,
        BRICK_CACHE,
        BRICK_IPC,
        BRICK_OBSERVABILITY,
        BRICK_UPLOADS,
        BRICK_RESILIENCY,
        BRICK_SEARCH,
        BRICK_PAY,
        BRICK_LLM,
        BRICK_SANDBOX,
        BRICK_WORKFLOWS,
        BRICK_DISCOVERY,
        BRICK_MCP,
        BRICK_MEMORY,
        BRICK_SKILLS,
        BRICK_ACCESS_MANIFEST,
        BRICK_CATALOG,
        BRICK_DELEGATION,
        BRICK_IDENTITY,
        BRICK_SHARE_LINK,
        BRICK_VERSIONING,
        BRICK_WORKSPACE,
        BRICK_PORTABILITY,
        BRICK_PARSERS,
        BRICK_SNAPSHOT,
        BRICK_TASK_MANAGER,
        BRICK_FEDERATION,
    }
)

# ---------------------------------------------------------------------------
# DeploymentProfile enum
# ---------------------------------------------------------------------------


class DeploymentProfile(StrEnum):
    """Deployment profile controlling which bricks are enabled by default.

    Profiles define capability tiers for different deployment targets:
    - cluster: Minimal multi-node — Raft + federation, no auth/PostgreSQL
    - embedded: MCU / WASM (<1 MB) — eventlog only
    - lite: Pi, Jetson, mobile (512 MB–4 GB) — core services, no LLM/Pay
    - sandbox: Agent sandbox (zero external services; SQLite + in-mem cache + BM25S; #3778)
    - full: Desktop, laptop (4–32 GB) — all bricks, local inference
    - cloud: k8s, serverless (unlimited) — all + federation + multi-tenant
    - remote: Client-side proxy — zero local bricks, NFS-client model (Issue #844)
    """

    CLUSTER = "cluster"
    EMBEDDED = "embedded"
    LITE = "lite"
    SANDBOX = "sandbox"
    FULL = "full"
    CLOUD = "cloud"
    REMOTE = "remote"

    def default_bricks(self) -> frozenset[str]:
        """Return the default set of enabled bricks for this profile."""
        return _PROFILE_BRICKS[self]

    def is_brick_enabled(self, brick: str) -> bool:
        """Check if a brick is enabled by default in this profile."""
        return brick in self.default_bricks()

    def default_drivers(self) -> frozenset[str]:
        """Return the default set of enabled drivers for this profile.

        Drivers are kernel-side dynamic backends (`backend_type` for
        `sys_setattr(DT_MOUNT)`).  Local CAS / path / connector backends
        are always available and are not listed here — see
        `rust/kernel/src/hal/backend_factory.rs::is_driver_enabled`.
        """
        return _PROFILE_DRIVERS[self]

    def is_driver_enabled(self, driver: str) -> bool:
        """Check if a driver is enabled by default in this profile."""
        return driver in self.default_drivers()

    def tuning(self) -> "ProfileTuning":
        """Return the performance tuning configuration for this profile.

        Issue #2071: Per-profile performance thresholds.
        """
        from nexus.lib.performance_tuning import resolve_profile_tuning

        return resolve_profile_tuning(self)


# ---------------------------------------------------------------------------
# Profile-to-brick mappings (frozen — immutable at runtime)
# ---------------------------------------------------------------------------

_CLUSTER_BRICKS: frozenset[str] = frozenset(
    {
        BRICK_IPC,
        BRICK_FEDERATION,
    }
)

_EMBEDDED_BRICKS: frozenset[str] = frozenset(
    {
        BRICK_EVENTLOG,
    }
)

_LITE_BRICKS: frozenset[str] = _EMBEDDED_BRICKS | frozenset(
    {
        BRICK_NAMESPACE,
        BRICK_PERMISSIONS,
        BRICK_CACHE,
        BRICK_IPC,
        BRICK_SCHEDULER,
    }
)

_SANDBOX_BRICKS: frozenset[str] = _LITE_BRICKS | frozenset(
    {
        BRICK_SEARCH,
        BRICK_MCP,
        BRICK_PARSERS,
    }
)

_FULL_BRICKS: frozenset[str] = _LITE_BRICKS | frozenset(
    {
        BRICK_SEARCH,
        BRICK_PAY,
        BRICK_LLM,
        BRICK_SKILLS,
        BRICK_SANDBOX,
        BRICK_WORKFLOWS,
        BRICK_DISCOVERY,
        BRICK_MCP,
        BRICK_MEMORY,
        BRICK_TASK_MANAGER,
        BRICK_OBSERVABILITY,
        BRICK_UPLOADS,
        BRICK_RESILIENCY,
        BRICK_ACCESS_MANIFEST,
        BRICK_CATALOG,
        BRICK_DELEGATION,
        BRICK_IDENTITY,
        BRICK_SHARE_LINK,
        BRICK_VERSIONING,
        BRICK_WORKSPACE,
        BRICK_PORTABILITY,
        BRICK_PARSERS,
        BRICK_SNAPSHOT,
    }
)

_CLOUD_BRICKS: frozenset[str] = _FULL_BRICKS | frozenset({BRICK_FEDERATION})

_REMOTE_BRICKS: frozenset[str] = frozenset()  # no local bricks — NFS-client model

_PROFILE_BRICKS: dict[DeploymentProfile, frozenset[str]] = {
    DeploymentProfile.CLUSTER: _CLUSTER_BRICKS,
    DeploymentProfile.EMBEDDED: _EMBEDDED_BRICKS,
    DeploymentProfile.LITE: _LITE_BRICKS,
    DeploymentProfile.SANDBOX: _SANDBOX_BRICKS,
    DeploymentProfile.FULL: _FULL_BRICKS,
    DeploymentProfile.CLOUD: _CLOUD_BRICKS,
    DeploymentProfile.REMOTE: _REMOTE_BRICKS,
}


# ---------------------------------------------------------------------------
# Profile-to-driver mappings (frozen — immutable at runtime)
# ---------------------------------------------------------------------------
#
# Driver gating mirrors brick gating: each profile lists the
# `backend_type` strings its active deployment can mount.  Local CAS /
# path / connector backends are always available (kernel default) and
# are not listed.  See `default_drivers()` for the surface; see
# `rust/kernel/src/hal/backend_factory.rs::is_driver_enabled` for the
# enforcement point.

# Cluster — Raft-replicated multi-node profile.  Federation needs the
# cross-node `remote` backend; storage / connectors stay disabled
# until an operator explicitly enables them per deployment.
_CLUSTER_DRIVERS: frozenset[str] = frozenset({DRIVER_REMOTE})

# Embedded / lite — single-machine, no remote storage.
_EMBEDDED_DRIVERS: frozenset[str] = frozenset()
_LITE_DRIVERS: frozenset[str] = frozenset()

# Sandbox — agent sandbox with Nostr cross-instance messaging + LLM
# connectors so chat-with-me / model calls work end-to-end.
_SANDBOX_DRIVERS: frozenset[str] = frozenset(
    {
        DRIVER_OPENAI,
        DRIVER_ANTHROPIC,
        DRIVER_NOSTR,
        DRIVER_CLI,
    }
)

# Full — desktop / laptop deployments. Adds cloud storage + connector
# integrations on top of the sandbox set.
_FULL_DRIVERS: frozenset[str] = _SANDBOX_DRIVERS | frozenset(
    {
        DRIVER_S3,
        DRIVER_GCS,
        DRIVER_GDRIVE,
        DRIVER_GMAIL,
        DRIVER_SLACK,
        DRIVER_X,
        DRIVER_HN,
        DRIVER_REMOTE,
    }
)

# Cloud — multi-tenant superset.
_CLOUD_DRIVERS: frozenset[str] = _FULL_DRIVERS

# Remote — NFS-client model: no local mounts, only the remote proxy.
_REMOTE_DRIVERS: frozenset[str] = frozenset({DRIVER_REMOTE})

_PROFILE_DRIVERS: dict[DeploymentProfile, frozenset[str]] = {
    DeploymentProfile.CLUSTER: _CLUSTER_DRIVERS,
    DeploymentProfile.EMBEDDED: _EMBEDDED_DRIVERS,
    DeploymentProfile.LITE: _LITE_DRIVERS,
    DeploymentProfile.SANDBOX: _SANDBOX_DRIVERS,
    DeploymentProfile.FULL: _FULL_DRIVERS,
    DeploymentProfile.CLOUD: _CLOUD_DRIVERS,
    DeploymentProfile.REMOTE: _REMOTE_DRIVERS,
}


def resolve_enabled_bricks(
    profile: DeploymentProfile,
    *,
    overrides: dict[str, bool] | None = None,
) -> frozenset[str]:
    """Resolve the effective set of enabled bricks.

    Starts with the profile's default brick set, then applies explicit
    overrides. Explicit overrides always win over profile defaults.

    Args:
        profile: The deployment profile providing defaults.
        overrides: Dict of brick_name -> enabled (True to force-enable,
                   False to force-disable). Unknown brick names raise ValueError.

    Returns:
        Frozen set of enabled brick names.
    """
    enabled = set(profile.default_bricks())

    if overrides:
        unknown = set(overrides.keys()) - ALL_BRICK_NAMES
        if unknown:
            raise ValueError(f"Unknown brick names in overrides: {unknown}")

        for brick_name, is_enabled in overrides.items():
            default_enabled = brick_name in profile.default_bricks()
            if is_enabled != default_enabled:
                action = "enabling" if is_enabled else "disabling"
                logger.warning(
                    "Brick override: %s '%s' (profile '%s' default: %s)",
                    action,
                    brick_name,
                    profile.value,
                    "enabled" if default_enabled else "disabled",
                )
            if is_enabled:
                enabled.add(brick_name)
            else:
                enabled.discard(brick_name)

    return frozenset(enabled)


def resolve_enabled_drivers(
    profile: DeploymentProfile,
    *,
    overrides: dict[str, bool] | None = None,
) -> frozenset[str]:
    """Resolve the effective set of enabled drivers for a profile.

    Mirrors :func:`resolve_enabled_bricks` for the driver dimension.
    Starts with the profile's default driver set, then applies explicit
    overrides.  Explicit overrides always win over profile defaults.

    Args:
        profile: The deployment profile providing defaults.
        overrides: Dict of driver_name -> enabled.  Unknown driver names
            raise ValueError.

    Returns:
        Frozen set of enabled driver names.
    """
    enabled = set(profile.default_drivers())

    if overrides:
        unknown = set(overrides.keys()) - ALL_DRIVER_NAMES
        if unknown:
            raise ValueError(f"Unknown driver names in overrides: {unknown}")

        for driver_name, is_enabled in overrides.items():
            default_enabled = driver_name in profile.default_drivers()
            if is_enabled != default_enabled:
                action = "enabling" if is_enabled else "disabling"
                logger.warning(
                    "Driver override: %s '%s' (profile '%s' default: %s)",
                    action,
                    driver_name,
                    profile.value,
                    "enabled" if default_enabled else "disabled",
                )
            if is_enabled:
                enabled.add(driver_name)
            else:
                enabled.discard(driver_name)

    return frozenset(enabled)
