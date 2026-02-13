"""Context manifest with deterministic pre-execution (Issue #1341).

Implements the Stripe Minions pattern: deterministically pre-execute relevant
sources and inject results into the agent's context BEFORE reasoning starts.

Public API:
    Models:
        - ``MCPToolSource`` — MCP tool pre-execution source
        - ``WorkspaceSnapshotSource`` — workspace snapshot source
        - ``FileGlobSource`` — file glob pattern source
        - ``MemoryQuerySource`` — memory/embedding query source
        - ``ContextSource`` — discriminated union of all source types
        - ``SourceResult`` — result of executing a single source
        - ``ManifestResult`` — aggregate result of manifest resolution
        - ``ManifestResolutionError`` — raised when required sources fail

    Resolver:
        - ``ManifestResolver`` — parallel source execution engine
        - ``SourceExecutor`` — protocol for source execution backends

    Template:
        - ``resolve_template`` — template variable substitution
        - ``ALLOWED_VARIABLES`` — whitelist of allowed template variables

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md
    - Issue #1341: Context manifest with deterministic pre-execution
"""

from nexus.services.context_manifest.models import (
    ContextSource,
    FileGlobSource,
    ManifestResolutionError,
    ManifestResult,
    MCPToolSource,
    MemoryQuerySource,
    SourceResult,
    WorkspaceSnapshotSource,
)
from nexus.services.context_manifest.resolver import ManifestResolver, SourceExecutor
from nexus.services.context_manifest.template import ALLOWED_VARIABLES, resolve_template

__all__ = [
    # Models
    "ContextSource",
    "FileGlobSource",
    "ManifestResolutionError",
    "ManifestResult",
    "MCPToolSource",
    "MemoryQuerySource",
    "SourceResult",
    "WorkspaceSnapshotSource",
    # Resolver
    "ManifestResolver",
    "SourceExecutor",
    # Template
    "ALLOWED_VARIABLES",
    "resolve_template",
]
