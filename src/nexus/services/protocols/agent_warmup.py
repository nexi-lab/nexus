"""Agent warmup service protocol (Issue #2172).

Defines the contract for structured agent initialization before accepting work.
The warmup service runs a sequence of steps (credentials, namespace, bricks,
caches, MCP, context) and gates the UNKNOWN → CONNECTED transition on
required step success.

Tier: System Service (TIER 3) — agent lifecycle infrastructure.

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md §2.4 (System Services)
    - Issue #2172: Agent warmup phase
"""

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.contracts.agent_warmup_types import WarmupResult, WarmupStep


@runtime_checkable
class AgentWarmupProtocol(Protocol):
    """Service contract for structured agent warmup.

    Implementations execute a sequence of warmup steps for an agent,
    gating the transition to CONNECTED on required step success.

    The warmup service is server-orchestrated: the server runs all steps
    on behalf of the agent. Steps run sequentially with per-step timeouts.

    Methods:
        warmup: Execute warmup steps for an agent.
    """

    async def warmup(
        self,
        agent_id: str,
        steps: "list[WarmupStep] | None" = None,
    ) -> "WarmupResult":
        """Execute warmup steps for an agent.

        Runs each step sequentially. Required steps that fail abort warmup
        and leave the agent in UNKNOWN state. Optional steps that fail are
        logged and skipped. On success, transitions the agent to CONNECTED.

        Args:
            agent_id: The agent to warm up.
            steps: Warmup steps to execute. If None, uses STANDARD_WARMUP.

        Returns:
            WarmupResult with success status and step-level detail.
        """
        ...
