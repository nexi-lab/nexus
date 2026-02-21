"""Discriminating auth provider — routes tokens by type.

Extracted from server/auth/factory.py. Routes tokens based on prefix:
- "sk-" prefix -> API key provider (static or database)
- JWT format   -> JWT/OIDC provider
"""

import base64
import json
import logging
from typing import Any

from nexus.auth.constants import API_KEY_PREFIX
from nexus.auth.providers.base import AuthProvider, AuthResult

logger = logging.getLogger(__name__)


class DiscriminatingAuthProvider(AuthProvider):
    """Auth provider with explicit token type discrimination.

    P0-1: Detect type explicitly by prefix/format, then route
    to the appropriate provider. Rejects ambiguous/unknown types early.
    """

    def __init__(
        self,
        api_key_provider: AuthProvider | None = None,
        jwt_provider: AuthProvider | None = None,
    ) -> None:
        self.api_key_provider = api_key_provider
        self.jwt_provider = jwt_provider

        providers = []
        if api_key_provider:
            providers.append("API keys")
        if jwt_provider:
            providers.append("JWT/OIDC")

        logger.info("Initialized DiscriminatingAuthProvider with: %s", ", ".join(providers))

    async def authenticate(self, token: str) -> AuthResult:
        """Authenticate with explicit token type discrimination."""
        if not token:
            return AuthResult(authenticated=False)

        if token.startswith(API_KEY_PREFIX):
            if self.api_key_provider:
                logger.debug("Routing to API key provider (prefix: sk-)")
                return await self.api_key_provider.authenticate(token)
            else:
                logger.error("UNAUTHORIZED: API key provided but no API key provider configured")
                return AuthResult(authenticated=False)
        else:
            if self.jwt_provider:
                if self._looks_like_jwt(token):
                    logger.debug("Routing to JWT/OIDC provider")
                    return await self.jwt_provider.authenticate(token)
                else:
                    logger.error("UNAUTHORIZED: Token format not recognized (not API key, not JWT)")
                    return AuthResult(authenticated=False)
            else:
                logger.error("UNAUTHORIZED: JWT token provided but no JWT provider configured")
                return AuthResult(authenticated=False)

    @staticmethod
    def _looks_like_jwt(token: str) -> bool:
        """Check if token looks like a JWT (3 base64url parts with alg header)."""
        parts = token.split(".")
        if len(parts) != 3:
            return False

        try:
            header_b64 = parts[0]
            padding = 4 - len(header_b64) % 4
            if padding != 4:
                header_b64 += "=" * padding
            header_json = base64.urlsafe_b64decode(header_b64)
            header = json.loads(header_json)
            return "alg" in header
        except Exception:
            return False

    async def validate_token(self, token: str) -> bool:
        result = await self.authenticate(token)
        return result.authenticated

    @property
    def session_factory(self) -> Any:
        """Get session_factory from API key provider for admin operations."""
        if self.api_key_provider and hasattr(self.api_key_provider, "session_factory"):
            return self.api_key_provider.session_factory
        return None

    def close(self) -> None:
        if self.api_key_provider:
            self.api_key_provider.close()
        if self.jwt_provider:
            self.jwt_provider.close()
