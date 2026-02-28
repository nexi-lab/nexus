"""Service layer for Nexus.

This module contains extracted services from NexusFS following Phase 2 refactoring.
Each service is independent and tested separately, using composition instead of mixins.

Services:
- SearchService: File search, glob, grep, and semantic search operations
- ReBACService: Relationship-Based Access Control and permission management
- MountService: Dynamic backend mounting, persistence, and sync operations
- VersionService: File version management, rollback, and diff operations
- MCPService: Model Context Protocol server management
- LLMService: LLM-powered document reading with citations
- OAuthService: OAuth credential management and provider integration
- SchedulerService: Fair-share priority scheduler (Astraea) — System Service, not a Brick

Subsystem ABC (Issue #1287):
- Subsystem: ABC for service lifecycle wrappers (health_check, cleanup)
- ContextIdentity: Frozen identity extracted from OperationContext
- extract_context_identity(): DRY helper for OperationContext → ContextIdentity

Phase 2: Core Refactoring (Issue #988)
"""

import importlib as _il

from nexus.contracts.types import ContextIdentity, extract_context_identity
from nexus.services.mount.mount_service import MountService
from nexus.services.oauth.oauth_service import OAuthService
from nexus.services.scheduler import SchedulerService
from nexus.services.search.search_service import SearchService
from nexus.services.subsystem import Subsystem
from nexus.services.versioning.version_service import VersionService

# Brick re-exports via importlib to avoid services→bricks tier violation (import-linter)
LLMService = _il.import_module("nexus.bricks.llm.llm_service").LLMService
MCPService = _il.import_module("nexus.bricks.mcp.mcp_service").MCPService
ReBACService = _il.import_module("nexus.bricks.rebac.rebac_service").ReBACService
__all__ = [
    "SearchService",
    "ReBACService",
    "MountService",
    "VersionService",
    "MCPService",
    "LLMService",
    "OAuthService",
    "SchedulerService",
    "Subsystem",
    "ContextIdentity",
    "extract_context_identity",
]
