"""Agent lifecycle event log — backward-compatible re-export.

The concrete SQLAlchemy implementation has been moved to
``nexus.storage.repositories.agent_event_log.SQLAlchemyAgentEventLog``
as part of Issue #2189 (brick storage Protocol extraction).

This module re-exports the concrete class under the original name
for backward compatibility with existing consumers.

For new code, prefer importing the Protocol from
``nexus.bricks.sandbox.protocols.AgentEventLogProtocol``
and the concrete implementation from
``nexus.storage.repositories.agent_event_log.SQLAlchemyAgentEventLog``.
"""

from nexus.storage.repositories.agent_event_log import SQLAlchemyAgentEventLog as AgentEventLog

__all__ = ["AgentEventLog"]
