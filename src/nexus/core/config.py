"""Configuration dataclasses for NexusFS subsystems.

Issue #1287: Extract NexusFS Domain Services from God Object.

These frozen dataclasses group related constructor parameters so that
subsystems receive a single config object instead of 5-10 keyword args.
Each subsystem may also define its own config if needed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CacheConfig:
    """Cache-related configuration for NexusFS kernel."""

    path_size: int = 512
    list_size: int = 1024
    kv_size: int = 256
    exists_size: int = 1024
    ttl_seconds: int | None = 300
    content_cache_size_mb: int = 256


@dataclass(frozen=True)
class SecurityConfig:
    """Security-related configuration for NexusFS kernel."""

    enforce_permissions: bool = True
    inherit_permissions: bool = True
    allow_admin_bypass: bool = False
    enforce_zone_isolation: bool = True
    audit_strict_mode: bool = True


@dataclass(frozen=True)
class FeatureFlags:
    """Feature flags for NexusFS kernel."""

    enable_workflows: bool = True
    enable_tiger_cache: bool = True
    enable_deferred_permissions: bool = True
    enable_distributed_events: bool = True
    enable_distributed_locks: bool = True
    enable_memory_paging: bool = True
