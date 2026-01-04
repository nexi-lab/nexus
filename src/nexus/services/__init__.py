"""Service layer for Nexus.

This module contains extracted services from NexusFS following Phase 2 refactoring.
Each service is independent and tested separately, using composition instead of mixins.

Services:
- SearchService: File search, glob, grep, and semantic search operations
- ReBACService: Relationship-Based Access Control and permission management

Phase 2: Core Refactoring (Issue #988)
"""

from nexus.services.rebac_service import ReBACService
from nexus.services.search_service import SearchService

__all__ = [
    "SearchService",
    "ReBACService",
]
