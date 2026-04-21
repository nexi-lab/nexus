"""OAuth credential lifecycle service — business logic + RPC surface.

Canonical location: ``nexus.bricks.auth.oauth.credential_service``.
Extracted from ``nexus.services.oauth_service`` (Issue #8B split).

This module is the single authoritative OAuth credential service.
``@rpc_expose`` decorators live directly on the brick methods (same
pattern as ReBACService, LLMService, MCPService).
"""

import builtins
import json
import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import DEFAULT_OAUTH_REDIRECT_URI
from nexus.lib.rpc_decorator import rpc_expose

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.contracts.cache_store import CacheStoreABC
    from nexus.contracts.types import OperationContext


class PKCEStateStore:
    """PKCE state store backed by CacheStoreABC.

    Uses CacheStoreABC for TTL-based ephemeral storage per
    KERNEL-ARCHITECTURE.md §2 (CacheStore pillar: ephemeral KV with TTL).

    PKCE state entries are JSON-serialized and stored with a TTL so that
    abandoned OAuth flows are automatically evicted. When no cache_store
    is provided, a NullCacheStore is used and PKCE state is effectively
    discarded (graceful degradation).
    """

    def __init__(
        self,
        cache_store: "CacheStoreABC | None" = None,
        ttl: int = 600,
    ) -> None:
        self._cache_store = cache_store
        self._ttl = ttl

    def _key(self, state: str) -> str:
        """Cache key for PKCE state token."""
        return f"oauth:pkce:{state}"

    async def save(self, state: str, pkce_data: dict[str, str]) -> None:
        """Store PKCE data keyed by state token."""
        if self._cache_store is None:
            return
        await self._cache_store.set(self._key(state), json.dumps(pkce_data).encode(), ttl=self._ttl)

    async def pop(self, state: str) -> dict[str, str] | None:
        """Retrieve and delete PKCE data (single-use). Returns None if missing/expired."""
        if self._cache_store is None:
            return None
        key = self._key(state)
        raw = await self._cache_store.get(key)
        if raw is None:
            return None
        await self._cache_store.delete(key)
        result: dict[str, str] = json.loads(raw)
        return result


class OAuthCredentialService:
    """OAuth credential lifecycle service with RPC surface.

    Handles provider discovery, authorization URLs, code exchange,
    credential listing/revocation/testing, and PKCE support.

    ``@rpc_expose`` decorators live directly on the brick (same pattern
    as ReBACService, LLMService, MCPService).  ``mcp_connect`` lives on
    MCPService where it belongs.
    """

    def __init__(
        self,
        oauth_factory: Any | None = None,
        token_manager: Any | None = None,
        *,
        database_url: str | None = None,
        oauth_config: Any | None = None,
        pkce_store: PKCEStateStore | None = None,
    ):
        self._oauth_factory = oauth_factory
        self._token_manager = token_manager
        self._database_url = database_url
        self._oauth_config = oauth_config
        self._pkce_store = pkce_store or PKCEStateStore()

    # =========================================================================
    # Public API: Provider Discovery
    # =========================================================================

    @rpc_expose(name="oauth_list_providers", description="List all available OAuth providers")
    async def list_providers(
        self,
        context: "OperationContext | None" = None,  # noqa: ARG002
    ) -> builtins.list[dict[str, Any]]:
        """List all available OAuth providers from configuration."""
        factory = self._get_oauth_factory()
        providers = []

        for provider_config in factory.list_providers():
            provider_dict = {
                "name": provider_config.name,
                "display_name": provider_config.display_name,
                "scopes": provider_config.scopes,
                "requires_pkce": provider_config.requires_pkce,
                "metadata": provider_config.metadata,
            }
            if provider_config.icon_url:
                provider_dict["icon_url"] = provider_config.icon_url
            providers.append(provider_dict)

        logger.info(f"Listed {len(providers)} OAuth providers")
        return providers

    # =========================================================================
    # Public API: OAuth Flow
    # =========================================================================

    @rpc_expose(
        name="oauth_get_auth_url", description="Get OAuth authorization URL for any provider"
    )
    async def get_auth_url(
        self,
        provider: str,
        redirect_uri: str = DEFAULT_OAUTH_REDIRECT_URI,
        scopes: builtins.list[str] | None = None,
    ) -> dict[str, Any]:
        """Get OAuth authorization URL for any provider."""
        import secrets

        logger.info(f"Generating OAuth authorization URL for provider={provider}")

        state = secrets.token_urlsafe(32)
        provider_instance = self._create_provider(provider, redirect_uri, scopes)
        self._register_provider(provider_instance)

        return await self._get_authorization_url_with_pkce_support(
            provider_instance, provider, state
        )

    @rpc_expose(
        name="oauth_exchange_code", description="Exchange OAuth authorization code for tokens"
    )
    async def exchange_code(
        self,
        provider: str,
        code: str,
        user_email: str | None = None,
        state: str | None = None,
        redirect_uri: str | None = None,
        code_verifier: str | None = None,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Exchange OAuth authorization code for tokens and store credentials."""
        from nexus.lib.context_utils import get_zone_id

        logger.info(
            f"Exchanging OAuth code for provider={provider}, "
            f"user_email={'provided' if user_email else 'will fetch'}"
        )

        provider_instance = self._create_provider(provider, redirect_uri)
        self._register_provider(provider_instance)

        try:
            factory = self._get_oauth_factory()
            config_name = self._map_provider_name(provider)
            provider_config = factory.get_provider_config(config_name)
            requires_pkce = provider_config and provider_config.requires_pkce

            if requires_pkce:
                pkce_verifier = await self._get_pkce_verifier(provider, code_verifier, state)
                credential = await provider_instance.exchange_code_pkce(code, pkce_verifier)
            else:
                credential = await provider_instance.exchange_code(code)
        except ValueError:
            raise
        except Exception as e:
            logger.error(f"Failed to exchange OAuth code: {e}")
            raise ValueError(f"Failed to exchange authorization code: {e}") from e

        if not user_email:
            user_email = await self._get_user_email_from_provider(provider_instance, credential)
            if not user_email:
                raise ValueError(
                    "user_email is required. Could not automatically fetch email from provider. "
                    "Please provide user_email parameter."
                )

        token_manager = self._get_token_manager()
        zone_id = get_zone_id(context)

        current_user_id = None
        if context:
            current_user_id = getattr(context, "user_id", None)
        created_by = current_user_id or user_email

        provider_name = provider_instance.provider_name
        # OAuthCredential is a frozen dataclass — in-place assignment raises
        # FrozenInstanceError.  Use ``dataclasses.replace`` to build a new
        # credential with the resolved user_email attached.
        import dataclasses as _dc

        credential = _dc.replace(credential, user_email=user_email)

        try:
            credential_id = await token_manager.store_credential(
                provider=provider_name,
                user_email=user_email,
                credential=credential,
                zone_id=zone_id,
                created_by=created_by,
                user_id=current_user_id,
            )

            logger.info(
                f"Successfully stored OAuth credential for {user_email} "
                f"(credential_id={credential_id})"
            )

            return {
                "credential_id": credential_id,
                "user_email": user_email,
                "expires_at": (
                    credential.expires_at.isoformat() if credential.expires_at else None
                ),
                "success": True,
            }
        except Exception as e:
            logger.error(f"Failed to store OAuth credential: {e}")
            raise ValueError(f"Failed to store credential: {e}") from e

    # =========================================================================
    # Public API: Credential Management
    # =========================================================================

    @rpc_expose(name="oauth_list_credentials", description="List all OAuth credentials")
    async def list_credentials(
        self,
        provider: str | None = None,
        include_revoked: bool = False,
        context: "OperationContext | None" = None,
    ) -> builtins.list[dict[str, Any]]:
        """List all OAuth credentials for the current user."""
        from nexus.lib.context_utils import get_zone_id

        token_manager = self._get_token_manager()
        if token_manager is None:
            return []
        zone_id = get_zone_id(context)

        current_user_id = None
        if context:
            current_user_id = getattr(context, "user_id", None)
        is_admin = context and getattr(context, "is_admin", False)

        credentials = await token_manager.list_credentials(
            zone_id=zone_id, user_id=current_user_id if not is_admin else None
        )

        result = []
        for cred in credentials:
            if not is_admin and current_user_id:
                cred_user_id = cred.get("user_id")
                cred_user_email = cred.get("user_email")
                if cred_user_id and cred_user_id != current_user_id:
                    continue
                if not cred_user_id and cred_user_email and cred_user_email != current_user_id:
                    continue
            if provider and cred["provider"] != provider:
                continue
            if not include_revoked and cred.get("revoked", False):
                continue
            result.append(cred)

        logger.info(
            f"Listed {len(result)} OAuth credentials for user_id={current_user_id}, "
            f"zone={zone_id}, provider={provider}"
        )
        return result

    @rpc_expose(name="oauth_revoke_credential", description="Revoke OAuth credential")
    async def revoke_credential(
        self,
        provider: str,
        user_email: str,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Revoke an OAuth credential."""
        from nexus.lib.context_utils import get_zone_id

        token_manager = self._get_token_manager()
        zone_id = get_zone_id(context)

        await self._check_credential_ownership(
            provider, user_email, zone_id, context, action="revoke"
        )

        try:
            success = await token_manager.revoke_credential(
                provider=provider,
                user_email=user_email,
                zone_id=zone_id,
            )

            if success:
                logger.info(f"Revoked OAuth credential for {provider}:{user_email}")
                return {"success": True}
            else:
                raise ValueError(f"Credential not found: {provider}:{user_email}")

        except Exception as e:
            logger.error(f"Failed to revoke credential: {e}")
            raise ValueError(f"Failed to revoke credential: {e}") from e

    async def delete_credentials(
        self,
        provider: str,
        user_email: str,
        zone_id: str | None = None,
    ) -> bool:
        """Mark the legacy credential as revoked in the token store.

        Called by OldStoreAdapter.delete() during `auth migrate --finalize`
        (#3741) to persist deletion to the underlying database rather than
        only removing the in-memory snapshot.

        Uses revoke (soft-delete) because the token manager has no hard-delete
        path; revoked rows are filtered from all live reads automatically.

        Returns True if the credential was found and revoked, False if absent.
        """
        from nexus.contracts.constants import ROOT_ZONE_ID

        token_manager = self._get_token_manager()
        if token_manager is None:
            return False

        effective_zone = zone_id if zone_id and zone_id != "root" else ROOT_ZONE_ID
        try:
            success = await token_manager.revoke_credential(
                provider=provider,
                user_email=user_email,
                zone_id=effective_zone,
            )
            if success:
                logger.info(
                    "delete_credentials: revoked legacy credential %s/%s (zone=%s) (#3741)",
                    provider,
                    user_email,
                    effective_zone,
                )
            return bool(success)
        except Exception as exc:
            logger.warning(
                "delete_credentials: failed to revoke %s/%s: %s (#3741)",
                provider,
                user_email,
                exc,
            )
            return False

    @rpc_expose(name="oauth_test_credential", description="Test OAuth credential validity")
    async def test_credential(
        self,
        provider: str,
        user_email: str,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Test if an OAuth credential is valid and can be refreshed."""
        from nexus.lib.context_utils import get_zone_id

        token_manager = self._get_token_manager()
        zone_id = get_zone_id(context)

        await self._check_credential_ownership(
            provider, user_email, zone_id, context, action="test"
        )

        try:
            token = await token_manager.get_valid_token(
                provider=provider,
                user_email=user_email,
                zone_id=zone_id,
            )

            if token:
                credentials = await token_manager.list_credentials(
                    zone_id=zone_id, user_email=user_email
                )
                cred_dict = next(
                    (c for c in credentials if c.get("user_email") == user_email),
                    None,
                )

                logger.info(f"OAuth credential test successful for {provider}:{user_email}")
                return {
                    "valid": True,
                    "refreshed": True,
                    "expires_at": cred_dict.get("expires_at") if cred_dict else None,
                }
            else:
                return {
                    "valid": False,
                    "error": "Could not retrieve valid token",
                }

        except Exception as e:
            logger.error(f"OAuth credential test failed: {e}")
            return {
                "valid": False,
                "error": str(e),
            }

    # =========================================================================
    # Internal helpers
    # =========================================================================

    async def _check_credential_ownership(
        self,
        provider: str,
        user_email: str,
        zone_id: str,
        context: "OperationContext | None",
        *,
        action: str = "access",
    ) -> None:
        """Verify that the current user owns the credential (or is admin)."""
        current_user_id = None
        if context:
            current_user_id = getattr(context, "user_id", None)
        is_admin = context and getattr(context, "is_admin", False)

        if is_admin or not current_user_id:
            return

        token_manager = self._get_token_manager()
        cred = await token_manager.get_credential(
            provider=provider, user_email=user_email, zone_id=zone_id
        )
        if not cred:
            return

        stored_user_id = cred.metadata.get("user_id") if cred.metadata else None
        stored_user_email = cred.user_email

        if stored_user_id and stored_user_id != current_user_id:
            raise ValueError(
                f"Permission denied: Cannot {action} credentials for {user_email}. "
                f"Only your own credentials can be {action}d."
            )
        if not stored_user_id and stored_user_email and stored_user_email != current_user_id:
            raise ValueError(
                f"Permission denied: Cannot {action} credentials for {user_email}. "
                f"Only your own credentials can be {action}d."
            )

    def _get_oauth_factory(self) -> Any:
        """Get or create OAuth provider factory."""
        if self._oauth_factory is None:
            from nexus.bricks.auth.oauth.factory import OAuthProviderFactory

            self._oauth_factory = OAuthProviderFactory(config=self._oauth_config)

        return self._oauth_factory

    def _get_token_manager(self) -> Any:
        """Get or create TokenManager instance.

        Returns None when no database URL is configured, allowing callers
        to gracefully degrade (e.g. return empty credential lists).
        """
        if self._token_manager is None:
            from nexus.bricks.auth.oauth.token_manager import TokenManager

            db_path = self._database_url

            if not db_path:
                logger.debug("TokenManager database not configured; OAuth credentials unavailable")
                return None

            logger.debug(f"TokenManager database URL resolved to: {db_path}")

            if db_path.startswith(("postgresql://", "mysql://", "sqlite://")):
                self._token_manager = TokenManager(db_url=db_path)
            else:
                self._token_manager = TokenManager(db_path=db_path)

        return self._token_manager

    def _map_provider_name(self, provider: str) -> str:
        """Map user-facing provider name to config provider name."""
        provider_name_map = {
            "google": "google-drive",
            "twitter": "x",
            "x": "x",
            "microsoft": "microsoft-onedrive",
            "microsoft-onedrive": "microsoft-onedrive",
        }
        return provider_name_map.get(provider, provider)

    def _create_provider(
        self,
        provider: str,
        redirect_uri: str | None = None,
        scopes: builtins.list[str] | None = None,
    ) -> Any:
        """Create OAuth provider instance using factory."""
        factory = self._get_oauth_factory()
        config_name = self._map_provider_name(provider)

        provider_instance = factory.create_provider(
            name=config_name,
            redirect_uri=redirect_uri,
            scopes=scopes,
        )

        logger.debug(f"Created provider {provider} using factory (config: {config_name})")
        return provider_instance

    def _register_provider(self, provider_instance: Any) -> None:
        """Register provider with TokenManager."""
        token_manager = self._get_token_manager()
        token_manager.register_provider(provider_instance.provider_name, provider_instance)

    async def _get_authorization_url_with_pkce_support(
        self,
        provider_instance: Any,
        provider: str,
        state: str,
    ) -> dict[str, Any]:
        """Get authorization URL with PKCE support if needed."""
        factory = self._get_oauth_factory()
        config_name = self._map_provider_name(provider)
        provider_config = factory.get_provider_config(config_name)
        requires_pkce = provider_config and provider_config.requires_pkce

        if requires_pkce:
            auth_url, pkce_data = provider_instance.get_authorization_url_with_pkce(state=state)
            await self._pkce_store.save(state, pkce_data)
            logger.info(
                "Generated OAuth authorization URL for %s with PKCE (state=%s)",
                provider,
                state,
            )
            return {
                "url": auth_url,
                "state": state,
                "pkce_data": pkce_data,
            }
        else:
            auth_url = provider_instance.get_authorization_url(state=state)
            logger.info("Generated OAuth authorization URL for %s (state=%s)", provider, state)
            return {
                "url": auth_url,
                "state": state,
            }

    async def _get_pkce_verifier(
        self,
        provider: str,
        code_verifier: str | None,
        state: str | None,
    ) -> str:
        """Get PKCE verifier from parameter or cache."""
        if code_verifier:
            return code_verifier

        if state:
            pkce_data = await self._pkce_store.pop(state)
            if pkce_data:
                verifier = pkce_data.get("code_verifier")
                if verifier:
                    return verifier

        raise ValueError(
            f"{provider} OAuth requires PKCE. Provide code_verifier parameter or use "
            "oauth_get_auth_url which returns pkce_data with code_verifier."
        )

    async def _get_user_email_from_provider(
        self, provider_instance: Any, credential: Any
    ) -> str | None:
        """Get user email from OAuth provider using the access token."""
        import httpx

        provider_name = provider_instance.provider_name

        try:
            if provider_name in ("google-drive", "gmail", "google-cloud-storage"):
                async with httpx.AsyncClient() as client:
                    try:
                        response = await client.get(
                            "https://oauth2.googleapis.com/tokeninfo",
                            params={"access_token": credential.access_token},
                        )
                        response.raise_for_status()
                        token_info = response.json()
                        if "email" in token_info:
                            email = token_info.get("email")
                            return str(email) if email else None
                    except httpx.HTTPError as e:
                        logger.debug("Google tokeninfo lookup failed: %s", e)

                    try:
                        response = await client.get(
                            "https://www.googleapis.com/oauth2/v2/userinfo",
                            headers={"Authorization": f"Bearer {credential.access_token}"},
                        )
                        response.raise_for_status()
                        user_info = response.json()
                        if "email" in user_info:
                            email = user_info.get("email")
                            return str(email) if email else None
                    except httpx.HTTPError as e:
                        logger.debug("Google userinfo lookup failed: %s", e)

            elif provider_name == "microsoft-onedrive":
                async with httpx.AsyncClient() as client:
                    try:
                        response = await client.get(
                            "https://graph.microsoft.com/v1.0/me",
                            headers={"Authorization": f"Bearer {credential.access_token}"},
                        )
                        response.raise_for_status()
                        user_info = response.json()
                        if "mail" in user_info:
                            email = user_info.get("mail")
                            return str(email) if email else None
                        elif "userPrincipalName" in user_info:
                            email = user_info.get("userPrincipalName")
                            return str(email) if email else None
                    except httpx.HTTPError as e:
                        logger.debug("Microsoft Graph user lookup failed: %s", e)

            elif provider_name == "x":
                async with httpx.AsyncClient() as client:
                    try:
                        response = await client.get(
                            "https://api.twitter.com/2/users/me",
                            headers={"Authorization": f"Bearer {credential.access_token}"},
                            params={"user.fields": "email"},
                        )
                        response.raise_for_status()
                        user_info = response.json()
                        if "data" in user_info and "email" in user_info["data"]:
                            email = user_info["data"].get("email")
                            return str(email) if email else None
                    except httpx.HTTPError as e:
                        logger.debug("X/Twitter user lookup failed: %s", e)

        except Exception as e:
            logger.warning(f"Failed to fetch user email from provider {provider_name}: {e}")

        return None
