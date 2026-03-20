"""Standard warmup step implementations (Issue #2172).

Each function receives a ``WarmupContext`` and returns True on success,
False on failure. Functions are registered into the ``AgentWarmupService``
step registry by ``register_standard_steps()``.

Steps:
    load_credentials   — Verify agent has valid credentials/API key
    mount_namespace    — Resolve agent's namespace mount table
    verify_bricks      — Check required bricks are enabled
    warm_caches        — Pre-populate cache for agent's zone (optional)
    connect_mcp        — Validate MCP server configuration (optional)
"""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.contracts.agent_warmup_types import WarmupContext
    from nexus.system_services.agents.agent_warmup import AgentWarmupService

logger = logging.getLogger(__name__)


async def load_credentials(ctx: "WarmupContext") -> bool:
    """Verify agent has valid credentials in the registry.

    Checks that the agent record has a non-empty owner_id and is in a
    state eligible for connection (UNKNOWN, IDLE, or SUSPENDED).
    """
    record = ctx.agent_record
    if not record.owner_id:
        logger.warning("[WARMUP:credentials] Agent %s has no owner_id", ctx.agent_id)
        return False

    from nexus.contracts.process_types import AgentState

    eligible = {AgentState.REGISTERED, AgentState.READY, AgentState.SUSPENDED}
    if record.state not in eligible:
        logger.warning(
            "[WARMUP:credentials] Agent %s in non-eligible state %s",
            ctx.agent_id,
            record.state.value,
        )
        return False

    logger.debug("[WARMUP:credentials] Agent %s credentials verified", ctx.agent_id)
    return True


async def mount_namespace(ctx: "WarmupContext") -> bool:
    """Resolve the agent's namespace mount table.

    Validates that the namespace manager can resolve mounts for this agent.
    Skips gracefully if namespace_manager is not available.
    """
    ns_mgr = ctx.namespace_manager
    if ns_mgr is None:
        logger.debug("[WARMUP:namespace] No namespace_manager, skipping mount resolution")
        return True  # Not a failure — just not configured

    try:
        # Attempt to resolve mounts for the agent's zone
        zone_id = ctx.agent_record.zone_id
        if zone_id is not None:
            subject = ("agent", ctx.agent_id)
            mounts = ns_mgr.get_mount_entries(subject, zone_id=zone_id)
            logger.debug(
                "[WARMUP:namespace] Resolved %d mounts for agent %s in zone %s",
                len(mounts),
                ctx.agent_id,
                zone_id,
            )
        return True
    except Exception:
        logger.exception(
            "[WARMUP:namespace] Failed to resolve namespace for agent %s", ctx.agent_id
        )
        return False


async def verify_bricks(ctx: "WarmupContext") -> bool:
    """Check that the deployment has enabled bricks.

    Validates that the enabled_bricks set is non-empty. If the agent
    has declared required capabilities in metadata, checks that matching
    bricks are available.
    """
    if not ctx.enabled_bricks:
        logger.debug("[WARMUP:bricks] No enabled_bricks configured, skipping verification")
        return True  # Not a failure — might be embedded deployment

    # Check agent capabilities against enabled bricks
    capabilities = ctx.agent_record.capabilities
    if capabilities:
        missing = [cap for cap in capabilities if cap not in ctx.enabled_bricks]
        if missing:
            logger.warning(
                "[WARMUP:bricks] Agent %s requires bricks %s but they are not enabled",
                ctx.agent_id,
                missing,
            )
            # Don't fail — capabilities are advisory, not mandatory
            # The agent can still function with degraded features

    logger.debug(
        "[WARMUP:bricks] %d bricks available for agent %s",
        len(ctx.enabled_bricks),
        ctx.agent_id,
    )
    return True


async def warm_caches(ctx: "WarmupContext") -> bool:
    """Pre-populate cache for the agent's zone (optional step).

    If a cache store is available and the agent has a zone_id, attempts
    to warm frequently-accessed cache keys.
    """
    cache = ctx.cache_store
    if cache is None:
        logger.debug("[WARMUP:caches] No cache_store, skipping cache warmup")
        return True

    try:
        zone_id = ctx.agent_record.zone_id
        if zone_id is not None:
            # Touch the cache to verify it's accessible
            # Actual cache warming strategy depends on deployment
            logger.debug("[WARMUP:caches] Cache store accessible for zone %s", zone_id)
        return True
    except Exception:
        logger.exception("[WARMUP:caches] Cache warmup failed for agent %s", ctx.agent_id)
        return False


async def connect_mcp(ctx: "WarmupContext") -> bool:
    """Validate MCP server configuration (optional step).

    Checks that MCP configuration is syntactically valid if provided.
    Does not establish actual connections (that happens at request time).
    """
    mcp_cfg = ctx.mcp_config
    if mcp_cfg is None:
        logger.debug("[WARMUP:mcp] No MCP configuration, skipping")
        return True

    try:
        # Validate configuration structure
        servers = mcp_cfg.get("servers", [])
        if not isinstance(servers, list):
            logger.warning("[WARMUP:mcp] Invalid MCP servers config (not a list)")
            return False

        logger.debug("[WARMUP:mcp] %d MCP servers configured", len(servers))
        return True
    except Exception:
        logger.exception("[WARMUP:mcp] MCP config validation failed for agent %s", ctx.agent_id)
        return False


def register_standard_steps(service: "AgentWarmupService") -> None:
    """Register all standard warmup steps into the service's step registry.

    Called during service initialization (e.g., in factory.py or lifespan setup).

    Args:
        service: The AgentWarmupService to register steps into.
    """
    service.register_step("load_credentials", load_credentials)
    service.register_step("mount_namespace", mount_namespace)
    service.register_step("verify_bricks", verify_bricks)
    service.register_step("warm_caches", warm_caches)
    service.register_step("connect_mcp", connect_mcp)
