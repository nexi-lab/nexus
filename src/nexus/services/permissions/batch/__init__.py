"""Batch permission operations.

Extracts bulk permission checking from EnhancedReBACManager:
- BulkPermissionChecker: Multi-phase bulk checking pipeline

Related: Issue #1459 Phase 15+
"""

from nexus.services.permissions.batch.bulk_checker import BulkPermissionChecker

__all__ = ["BulkPermissionChecker"]
