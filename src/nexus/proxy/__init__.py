"""Proxy brick — transparent edge-to-cloud forwarding with offline queue.

Public API
----------
- ``ProxyBrick``           — base proxy (subclass for custom protocols)
- ``ProxyVFSBrick``        — proxy for VFSOperations protocol
- ``ProxySchedulerBrick``  — proxy for SchedulerProtocol
- ``ProxyAgentRegistryBrick`` — proxy for AgentRegistryProtocol
- ``ProxyBrickConfig``     — immutable configuration dataclass
- ``create_proxy_brick()`` — convenience factory

Errors
------
- ``ProxyError``           — base exception
- ``OfflineQueuedError``   — operation queued for later replay
- ``CircuitOpenError``     — circuit breaker is open
- ``QueueReplayError``     — queued operation failed during replay
- ``RemoteCallError``      — remote call failed after retries
"""

from nexus.proxy.brick import (
    ProxyAgentRegistryBrick,
    ProxyBrick,
    ProxySchedulerBrick,
    ProxyVFSBrick,
)
from nexus.proxy.circuit_breaker import CircuitState
from nexus.proxy.config import ProxyBrickConfig
from nexus.proxy.errors import (
    CircuitOpenError,
    OfflineQueuedError,
    ProxyError,
    QueueReplayError,
    RemoteCallError,
)
from nexus.proxy.queue_protocol import InMemoryQueue, OfflineQueueProtocol, QueuedOperation
from nexus.proxy.replay_engine import ReplayEngine

__all__ = [
    "ProxyBrick",
    "ProxyVFSBrick",
    "ProxySchedulerBrick",
    "ProxyAgentRegistryBrick",
    "ProxyBrickConfig",
    "ProxyError",
    "OfflineQueuedError",
    "CircuitOpenError",
    "QueueReplayError",
    "RemoteCallError",
    "CircuitState",
    "InMemoryQueue",
    "OfflineQueueProtocol",
    "QueuedOperation",
    "ReplayEngine",
    "create_proxy_brick",
]

_PROTOCOL_MAP: dict[str, type[ProxyBrick]] = {
    "vfs": ProxyVFSBrick,
    "scheduler": ProxySchedulerBrick,
    "agent_registry": ProxyAgentRegistryBrick,
}


def create_proxy_brick(
    protocol: str,
    config: ProxyBrickConfig,
) -> ProxyBrick:
    """Create a ProxyBrick for the given protocol.

    Parameters
    ----------
    protocol:
        One of ``"vfs"``, ``"scheduler"``, ``"agent_registry"``.
    config:
        Proxy configuration.

    Raises
    ------
    ValueError:
        If *protocol* is not recognised.
    """
    cls = _PROTOCOL_MAP.get(protocol)
    if cls is None:
        valid = ", ".join(sorted(_PROTOCOL_MAP))
        raise ValueError(f"Unknown protocol {protocol!r}; choose from: {valid}")
    return cls(config)
