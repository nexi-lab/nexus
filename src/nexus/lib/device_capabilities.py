"""Device capability detection and profile suggestion.

Issue #1708: Auto-detect RAM, CPU cores, and GPU availability at startup
to suggest the appropriate deployment profile (embedded/lite/full/cloud).

Detection is performed once at startup via ``detect_capabilities()``.
When ``NEXUS_PROFILE=auto``, the suggested profile drives brick gating.

Environment variable overrides (for CI / containers):
- ``NEXUS_HAS_GPU``: "true"/"false" — skip GPU probe
- ``NEXUS_MEMORY_MB``: integer — override detected RAM
- ``NEXUS_CPU_CORES``: integer — override detected CPU count

Lego Architecture reference: Part 10 — Edge Deployment.
"""

import functools
import logging
import os
import platform
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.contracts.deployment_profile import DeploymentProfile

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DeviceCapabilities — immutable snapshot of detected hardware
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceCapabilities:
    """Frozen snapshot of device hardware capabilities.

    Detected once at startup by ``detect_capabilities()``.
    """

    memory_mb: int
    cpu_cores: int = 1
    has_gpu: bool = False
    has_network: bool = True
    has_persistent_storage: bool = True


# ---------------------------------------------------------------------------
# BrickRequirement — minimum hardware for each optional brick
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BrickRequirement:
    """Minimum device capabilities required to run a brick."""

    min_memory_mb: int = 0
    requires_gpu: bool = False
    requires_network: bool = False


BRICK_REQUIREMENTS: dict[str, BrickRequirement] = {
    # Feature bricks (Tier 2) — gated by capabilities
    "search": BrickRequirement(min_memory_mb=512),
    "llm": BrickRequirement(min_memory_mb=1024),
    "pay": BrickRequirement(min_memory_mb=128, requires_network=True),
    "sandbox": BrickRequirement(min_memory_mb=256),
    "workflows": BrickRequirement(min_memory_mb=128),
    "discovery": BrickRequirement(min_memory_mb=64),
    "mcp": BrickRequirement(min_memory_mb=64),
    "memory": BrickRequirement(min_memory_mb=128),
    "skills": BrickRequirement(min_memory_mb=128),
    "federation": BrickRequirement(min_memory_mb=256, requires_network=True),
    # Infrastructure bricks
    "cache": BrickRequirement(min_memory_mb=64),
    "ipc": BrickRequirement(min_memory_mb=64),
    "observability": BrickRequirement(min_memory_mb=64),
    "uploads": BrickRequirement(min_memory_mb=64),
    "resiliency": BrickRequirement(min_memory_mb=32),
    # System bricks (lightweight — always meet requirements in practice)
    "eventlog": BrickRequirement(min_memory_mb=32),
    "namespace": BrickRequirement(min_memory_mb=32),
    "permissions": BrickRequirement(min_memory_mb=32),
    "scheduler": BrickRequirement(min_memory_mb=32),
    # Additional feature bricks
    "access_manifest": BrickRequirement(min_memory_mb=32),
    "catalog": BrickRequirement(min_memory_mb=64),
    "delegation": BrickRequirement(min_memory_mb=32),
    "identity": BrickRequirement(min_memory_mb=32),
    "share_link": BrickRequirement(min_memory_mb=32),
    "versioning": BrickRequirement(min_memory_mb=32),
    "workspace": BrickRequirement(min_memory_mb=64),
    "portability": BrickRequirement(min_memory_mb=64),
    "parsers": BrickRequirement(min_memory_mb=128),
    "snapshot": BrickRequirement(min_memory_mb=64),
    "acp": BrickRequirement(min_memory_mb=64),
    "task_manager": BrickRequirement(min_memory_mb=32),
}

# ---------------------------------------------------------------------------
# Detection functions
# ---------------------------------------------------------------------------


def get_system_memory_mb() -> int:
    """Detect total system memory in megabytes.

    Priority:
    1. ``NEXUS_MEMORY_MB`` environment variable
    2. ``psutil.virtual_memory().total``
    3. Platform-specific fallback (macOS sysctl, Linux /proc/meminfo)
    4. Conservative default: 4096 MB
    """
    env_override = os.environ.get("NEXUS_MEMORY_MB")
    if env_override is not None:
        try:
            return int(env_override)
        except ValueError:
            logger.warning("Invalid NEXUS_MEMORY_MB='%s', falling back to detection", env_override)

    try:
        import psutil

        return int(psutil.virtual_memory().total / (1024 * 1024))
    except ImportError:
        pass

    import subprocess

    try:
        system = platform.system()
        if system == "Darwin":
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return int(int(result.stdout.strip()) / (1024 * 1024))
        elif system == "Linux":
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return int(kb / 1024)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        logger.debug("Platform memory detection failed: %s", exc)

    return 4096


def get_available_memory_mb() -> int:
    """Detect available (free) system memory in megabytes.

    Priority:
    1. ``psutil.virtual_memory().available``
    2. Conservative default: 2048 MB
    """
    try:
        import psutil

        return int(psutil.virtual_memory().available / (1024 * 1024))
    except ImportError:
        pass

    return 2048


def get_cpu_cores() -> int:
    """Detect number of CPU cores.

    Priority:
    1. ``NEXUS_CPU_CORES`` environment variable
    2. ``os.cpu_count()``
    3. Fallback: 1
    """
    env_override = os.environ.get("NEXUS_CPU_CORES")
    if env_override is not None:
        try:
            return int(env_override)
        except ValueError:
            logger.warning("Invalid NEXUS_CPU_CORES='%s', falling back to detection", env_override)

    count = os.cpu_count()
    return count if count is not None else 1


def has_gpu() -> bool:
    """Detect whether a GPU is available.

    Priority:
    1. ``NEXUS_HAS_GPU`` environment variable ("true"/"false")
    2. ``shutil.which("nvidia-smi")`` path check
    3. ``nvidia-smi`` subprocess probe
    4. Default: False
    """
    env_override = os.environ.get("NEXUS_HAS_GPU")
    if env_override is not None:
        return env_override.lower() in ("true", "1", "yes")

    import shutil

    if shutil.which("nvidia-smi") is None:
        return False

    import subprocess

    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        return False


@functools.lru_cache(maxsize=1)
def detect_capabilities() -> DeviceCapabilities:
    """One-shot detection of device capabilities. Logs timing.

    Cached after first call — hardware doesn't change during a process.
    Called at startup when ``NEXUS_PROFILE=auto`` or when
    ``warn_if_profile_exceeds_device()`` needs capabilities.
    """
    t0 = time.perf_counter()

    caps = DeviceCapabilities(
        memory_mb=get_system_memory_mb(),
        cpu_cores=get_cpu_cores(),
        has_gpu=has_gpu(),
    )

    elapsed_ms = (time.perf_counter() - t0) * 1000
    logger.info(
        "Device capabilities detected in %.1fms: RAM=%dMB, cores=%d, GPU=%s",
        elapsed_ms,
        caps.memory_mb,
        caps.cpu_cores,
        caps.has_gpu,
    )
    return caps


# ---------------------------------------------------------------------------
# Profile suggestion and brick filtering
# ---------------------------------------------------------------------------

# Profile tier indices for comparison
# remote=-2: never auto-detected, always explicit (Issue #844)
_PROFILE_INDEX: dict[str, int] = {
    "remote": -2,
    "embedded": 0,
    "lite": 1,
    "full": 2,
    "cloud": 3,
}


def suggest_profile(caps: DeviceCapabilities) -> "DeploymentProfile":
    """Map device capabilities to a suggested deployment profile.

    Memory thresholds (conservative):
    - <512 MB   → EMBEDDED
    - 512–4095 MB → LITE
    - 4096–32767 MB → FULL
    - >=32768 MB  → CLOUD
    """
    from nexus.contracts.deployment_profile import DeploymentProfile

    if caps.memory_mb < 512:
        return DeploymentProfile.EMBEDDED
    if caps.memory_mb < 4096:
        return DeploymentProfile.LITE
    if caps.memory_mb < 32768:
        return DeploymentProfile.FULL
    return DeploymentProfile.CLOUD


def bricks_for_device(caps: DeviceCapabilities) -> frozenset[str]:
    """Filter BRICK_REQUIREMENTS by device capabilities.

    Returns the set of brick names whose requirements are met by *caps*.
    """
    result: set[str] = set()
    for brick_name, req in BRICK_REQUIREMENTS.items():
        if caps.memory_mb < req.min_memory_mb:
            continue
        if req.requires_gpu and not caps.has_gpu:
            continue
        if req.requires_network and not caps.has_network:
            continue
        result.add(brick_name)
    return frozenset(result)


def warn_if_profile_exceeds_device(
    profile: "DeploymentProfile",
    caps: DeviceCapabilities,
) -> None:
    """Log WARNING if the explicit profile may exceed device capabilities.

    Called from connect() and server when profile is explicitly set
    (not auto) so users get early feedback about potential issues.
    """
    suggested = suggest_profile(caps)
    profile_idx = _PROFILE_INDEX.get(profile.value, 2)
    suggested_idx = _PROFILE_INDEX.get(suggested.value, 2)

    if profile_idx > suggested_idx:
        logger.warning(
            "Profile '%s' may exceed device capabilities (detected: RAM=%dMB, suggested: '%s')",
            profile,
            caps.memory_mb,
            suggested,
        )
