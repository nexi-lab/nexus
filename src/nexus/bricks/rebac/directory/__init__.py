"""Directory permission operations — Leopard-style pre-materialization.

Extracts directory-related operations from ReBACManager:
- DirectoryExpander: path detection, grant expansion, descendant queries

Related: Issue #1459 Phase 13, Leopard pattern
"""

from nexus.bricks.rebac.directory.expander import DirectoryExpander

__all__ = ["DirectoryExpander"]
