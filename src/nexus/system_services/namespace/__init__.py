"""Namespace fork service domain — SYSTEM tier (Issue #1273).

Agent namespace forking for speculative execution.
Plan 9 ``rfork(RFNAMEG)`` inspired: fork, explore, merge or discard.
"""

from nexus.system_services.namespace.agent_namespace import AgentNamespace
from nexus.system_services.namespace.descendant_access import DescendantAccessChecker
from nexus.system_services.namespace.namespace_fork_service import AgentNamespaceForkService

__all__ = [
    "AgentNamespace",
    "AgentNamespaceForkService",
    "DescendantAccessChecker",
]
