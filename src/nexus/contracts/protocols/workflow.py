"""Workflow-related protocol interfaces — canonical home is now contracts/.

Re-exports from ``nexus.contracts.workflow_types`` for any remaining
consumers that import from here.

See: NEXUS-LEGO-ARCHITECTURE.md §2.4, §3.3
"""

from nexus.contracts.workflow_types import (
    MetadataStoreProtocol,
    NexusOperationsProtocol,
)

__all__ = ["MetadataStoreProtocol", "NexusOperationsProtocol"]
