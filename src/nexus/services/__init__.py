"""Service layer for Nexus.

This module contains orchestration services that coordinate bricks.
Individual services live in their canonical brick locations:

- nexus.bricks.llm.llm_service.LLMService
- nexus.bricks.mcp.mcp_service.MCPService
- nexus.bricks.mount.mount_service.MountService
- nexus.bricks.auth.oauth.credential_service.OAuthCredentialService
- nexus.bricks.rebac.rebac_service.ReBACService
- nexus.bricks.versioning.version_service.VersionService
- nexus.services.search.search_service.SearchService
- nexus.contracts.types.ContextIdentity
- nexus.contracts.types.extract_context_identity
"""
