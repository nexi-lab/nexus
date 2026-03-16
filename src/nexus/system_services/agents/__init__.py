"""Agent service domain -- SYSTEM tier.

Canonical location for agent registry, provisioning, and lifecycle.
"""

from nexus.system_services.agents.agent_registry import AgentRegistry, AsyncAgentRegistry

__all__ = [
    "AgentRegistry",
    "AsyncAgentRegistry",
]
