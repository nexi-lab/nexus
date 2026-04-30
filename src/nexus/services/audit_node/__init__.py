"""Audit-node service — collect/gather audit traces from production zones.

An audit-node is a federation peer that joins production zones as a
**raft learner** (vote-only=false, replicate=true).  Because the
audit DT_STREAM lives in a WAL-replicated stream
(``/{zone}/audit/traces/``), every committed record reaches the
audit-node's local metastore through raft replication.

The audit-node does NOT register an ``AuditHook`` of its own — it
only consumes the streams produced by production nodes.  This keeps
the audit-node's local zone free of self-generated noise.

Architectural references: ``docs/architecture/KERNEL-ARCHITECTURE.md``
§ federation, ``sudowork-2/docs/tech/nexus-integration-architecture.md``
§ Audit Trace.
"""

from .service import AuditCheckpoint, AuditNode

__all__ = ["AuditNode", "AuditCheckpoint"]
