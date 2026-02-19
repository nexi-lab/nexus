"""Sandbox metadata repository — backward-compatible re-export.

The concrete SQLAlchemy implementation has been moved to
``nexus.storage.repositories.sandbox.SQLAlchemySandboxRepository``
as part of Issue #2189 (brick storage Protocol extraction).

This module re-exports the concrete class under the original name
for backward compatibility with existing consumers.

For new code, prefer importing the Protocol from
``nexus.bricks.sandbox.protocols.SandboxRepositoryProtocol``
and the concrete implementation from
``nexus.storage.repositories.sandbox.SQLAlchemySandboxRepository``.
"""

from nexus.storage.repositories.sandbox import SQLAlchemySandboxRepository as SandboxRepository

__all__ = ["SandboxRepository"]
