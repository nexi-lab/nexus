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
- SkillService: Skill lifecycle management and governance

Phase 2: Core Refactoring (Issue #988)
"""

from nexus.services.llm_service import LLMService
from nexus.services.mcp_service import MCPService
from nexus.services.mount_service import MountService
from nexus.services.oauth_service import OAuthService
from nexus.services.rebac_service import ReBACService
from nexus.services.search_service import SearchService
from nexus.services.skill_service import SkillService
from nexus.services.version_service import VersionService

__all__ = [
    "SearchService",
    "ReBACService",
    "MountService",
    "VersionService",
    "MCPService",
    "LLMService",
    "OAuthService",
    "SkillService",
]
