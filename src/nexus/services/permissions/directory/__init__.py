"""Directory permission operations — Leopard-style pre-materialization.

Extracts directory-related operations from EnhancedReBACManager:
- DirectoryExpander: path detection, grant expansion, descendant queries

Related: Issue #1459 Phase 13, Leopard pattern
"""

from nexus.services.permissions.directory.expander import DirectoryExpander

__all__ = ["DirectoryExpander"]
