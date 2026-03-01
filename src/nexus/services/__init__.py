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
- OAuthCredentialService: OAuth credential management (brick)
- ContextIdentity: Frozen identity extracted from OperationContext
- extract_context_identity(): DRY helper for OperationContext → ContextIdentity

Phase 2: Core Refactoring (Issue #988)
"""

import importlib as _il

from nexus.bricks.versioning.version_service import VersionService
from nexus.contracts.types import ContextIdentity, extract_context_identity
from nexus.services.search.search_service import SearchService

# Brick re-exports via importlib to avoid services→bricks tier violation (import-linter)
LLMService = _il.import_module("nexus.bricks.llm.llm_service").LLMService
MCPService = _il.import_module("nexus.bricks.mcp.mcp_service").MCPService
MountService = _il.import_module("nexus.bricks.mount.mount_service").MountService
OAuthCredentialService = _il.import_module(
    "nexus.bricks.auth.oauth.credential_service"
).OAuthCredentialService
ReBACService = _il.import_module("nexus.bricks.rebac.rebac_service").ReBACService
__all__ = [
    "SearchService",
    "ReBACService",
    "MountService",
    "VersionService",
    "MCPService",
    "LLMService",
    "OAuthCredentialService",
    "ContextIdentity",
    "extract_context_identity",
]
