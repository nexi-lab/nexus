"""OAuth/OIDC authentication provider for SSO integration."""

import logging
import time
from typing import Any

import requests
from authlib.jose import JoseError, JsonWebKey, jwt

from nexus.bricks.auth.providers.base import AuthProvider, AuthResult

logger = logging.getLogger(__name__)

# Security constants
ALLOWED_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"]
CLOCK_SKEW_SECONDS = 300  # +/- 5 minutes
JWKS_CACHE_TTL = 3600  # 1 hour
OIDC_REQUEST_TIMEOUT = 10  # HTTP request timeout for JWKS/discovery fetches


class OIDCAuth(AuthProvider):
    """OAuth/OIDC authentication provider.

    Validates JWT tokens from external identity providers (Google, GitHub,
    Microsoft, Okta, Auth0, etc.).

    Security:
    - Validates token signature using provider's public keys (JWKS)
    - Enforces RS256/ES256 only (NO HS256)
    - Validates iss, aud, exp, nbf, iat with clock skew tolerance
    - JWKS caching with 1-hour TTL
    """

    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_uri: str | None = None,
        subject_type: str = "user",
        subject_id_claim: str = "sub",
        zone_id_claim: str | None = "org_id",
        admin_emails: list[str] | None = None,
        allow_default_zone: bool = False,
        require_zone: bool = False,
    ) -> None:
        self.issuer = issuer
        self.audience = audience
        self.jwks_uri = jwks_uri or self._discover_jwks_uri(issuer)
        self.subject_type = subject_type
        self.subject_id_claim = subject_id_claim
        self.zone_id_claim = zone_id_claim
        self.admin_emails = set(admin_emails or [])
        self.allow_default_zone = allow_default_zone
        self.require_zone = require_zone

        self._jwks_cache: dict[str, Any] | None = None
        self._jwks_cache_time: float = 0

        logger.info("Initialized OIDCAuth for issuer: %s (JWKS: %s)", issuer, self.jwks_uri)

    def _discover_jwks_uri(self, issuer: str) -> str:
        """Discover JWKS URI from OIDC discovery endpoint.

        For well-known issuers, returns the direct JWKS endpoint.
        For unknown issuers, performs OIDC discovery to find the ``jwks_uri``.
        """
        # Direct JWKS endpoints for well-known issuers
        patterns: dict[str, str] = {
            "https://accounts.google.com": "https://www.googleapis.com/oauth2/v3/certs",
            "https://login.microsoftonline.com": f"{issuer}/discovery/v2.0/keys",
            "https://github.com": "https://token.actions.githubusercontent.com/.well-known/jwks",
        }
        if issuer in patterns:
            return patterns[issuer]

        # Generic issuers: fetch the OpenID discovery document and extract jwks_uri
        discovery_url = f"{issuer}/.well-known/openid-configuration"
        try:
            response = requests.get(discovery_url, timeout=OIDC_REQUEST_TIMEOUT)
            response.raise_for_status()
            config = response.json()
            jwks_uri = config.get("jwks_uri")
            if jwks_uri:
                return str(jwks_uri)
            raise ValueError(f"No jwks_uri in discovery document from {discovery_url}")
        except Exception as e:
            logger.warning("OIDC discovery failed for %s: %s — falling back to /jwks", issuer, e)
            return f"{issuer}/.well-known/jwks.json"

    def _fetch_jwks(self) -> dict[str, Any]:
        """Fetch JWKS from provider with caching (sync version)."""
        now = time.time()
        if self._jwks_cache and (now - self._jwks_cache_time) < JWKS_CACHE_TTL:
            return self._jwks_cache

        try:
            logger.info("Fetching JWKS from %s", self.jwks_uri)
            response = requests.get(self.jwks_uri, timeout=OIDC_REQUEST_TIMEOUT)
            response.raise_for_status()
            jwks = response.json()

            self._jwks_cache = jwks
            self._jwks_cache_time = now

            result: dict[str, Any] = dict(jwks)
            return result
        except Exception as e:
            logger.error("Failed to fetch JWKS from %s: %s", self.jwks_uri, e, exc_info=True)
            raise ValueError(f"INDETERMINATE: Cannot fetch JWKS - {e}") from e

    async def _fetch_jwks_async(self) -> dict[str, Any]:
        """Non-blocking JWKS fetch (Decision #14).

        Uses asyncio.to_thread to avoid blocking the event loop
        during the HTTP request to the JWKS endpoint.
        """
        import asyncio

        now = time.time()
        if self._jwks_cache and (now - self._jwks_cache_time) < JWKS_CACHE_TTL:
            return self._jwks_cache

        try:
            logger.info("Fetching JWKS (async) from %s", self.jwks_uri)
            response = await asyncio.to_thread(
                requests.get, self.jwks_uri, timeout=OIDC_REQUEST_TIMEOUT
            )
            response.raise_for_status()
            jwks = response.json()

            self._jwks_cache = jwks
            self._jwks_cache_time = now

            result: dict[str, Any] = dict(jwks)
            return result
        except Exception as e:
            logger.error("Failed to fetch JWKS from %s: %s", self.jwks_uri, e, exc_info=True)
            raise ValueError(f"INDETERMINATE: Cannot fetch JWKS - {e}") from e

    def verify_token(self, token: str) -> dict[str, Any]:
        """Verify and decode OIDC ID token with security validation.

        Raises:
            ValueError: If token is invalid.
        """
        try:
            header = jwt.decode_header(token)
            alg = header.get("alg")
            kid = header.get("kid")

            if alg not in ALLOWED_ALGORITHMS:
                raise ValueError(
                    f"UNAUTHORIZED: Algorithm {alg} not allowed. "
                    f"Must be one of {ALLOWED_ALGORITHMS}"
                )

            jwks = self._fetch_jwks()
            keys = jwks.get("keys", [])

            public_key = None
            if kid:
                for key_data in keys:
                    if key_data.get("kid") == kid:
                        public_key = JsonWebKey.import_key(key_data)
                        break
                if not public_key:
                    raise ValueError(f"UNAUTHORIZED: Key ID {kid} not found in JWKS")
            else:
                if keys:
                    logger.warning("Token has no kid - using first JWKS key")
                    public_key = JsonWebKey.import_key(keys[0])
                else:
                    raise ValueError("UNAUTHORIZED: No keys in JWKS")

            now = int(time.time())
            claims_options = {
                "iss": {"essential": True, "value": self.issuer},
                "aud": {"essential": True, "value": self.audience},
                "exp": {"essential": True, "validate": lambda v: v > (now - CLOCK_SKEW_SECONDS)},
                "iat": {"essential": True, "validate": lambda v: v <= (now + CLOCK_SKEW_SECONDS)},
                "nbf": {"essential": False, "validate": lambda v: v <= (now + CLOCK_SKEW_SECONDS)},
            }

            claims = jwt.decode(token, public_key, claims_options=claims_options)
            claims.validate()

            exp = claims.get("exp")
            iat = claims.get("iat")
            nbf = claims.get("nbf")

            if exp and exp < (now - CLOCK_SKEW_SECONDS):
                raise ValueError(f"UNAUTHORIZED: Token expired at {exp}")
            if iat and iat > (now + CLOCK_SKEW_SECONDS):
                raise ValueError(f"UNAUTHORIZED: Token issued in future: {iat}")
            if nbf and nbf > (now + CLOCK_SKEW_SECONDS):
                raise ValueError(f"UNAUTHORIZED: Token not valid before {nbf}")

            result: dict[str, Any] = dict(claims)
            return result

        except JoseError as e:
            raise ValueError(f"UNAUTHORIZED: Invalid OIDC token - {e}") from e

    async def authenticate(self, token: str) -> AuthResult:
        """Authenticate using OIDC ID token."""
        try:
            # Decision #14: Pre-warm JWKS cache asynchronously to avoid
            # blocking the event loop during the HTTP request.
            await self._fetch_jwks_async()
            claims = self.verify_token(token)

            subject_id = claims.get(self.subject_id_claim)
            if not subject_id:
                logger.error(
                    "UNAUTHORIZED: Token missing required claim: %s", self.subject_id_claim
                )
                return AuthResult(authenticated=False)

            provider_prefix = self._extract_provider_prefix(claims.get("iss", ""))
            subject_id = f"{provider_prefix}:{subject_id}"

            zone_id = None
            if self.zone_id_claim:
                zone_id = claims.get(self.zone_id_claim)

            if self.require_zone and not zone_id and not self.allow_default_zone:
                logger.error(
                    "UNAUTHORIZED: Zone required but not found in token. "
                    "Claim '%s' missing or empty.",
                    self.zone_id_claim,
                )
                return AuthResult(authenticated=False)

            email = claims.get("email")
            is_admin = email in self.admin_emails if email else False

            return AuthResult(
                authenticated=True,
                subject_type=self.subject_type,
                subject_id=subject_id,
                zone_id=zone_id,
                is_admin=is_admin,
                metadata={
                    "email": email,
                    "name": claims.get("name"),
                    "picture": claims.get("picture"),
                    "provider": provider_prefix,
                },
            )
        except ValueError as e:
            logger.warning("OIDC authentication failed: %s", e)
            return AuthResult(authenticated=False)

    async def validate_token(self, token: str) -> bool:
        try:
            self.verify_token(token)
            return True
        except ValueError:
            return False

    def close(self) -> None:
        pass

    def _extract_provider_prefix(self, issuer: str) -> str:
        """Extract provider name from issuer URL."""
        if "google.com" in issuer:
            return "google"
        elif "github.com" in issuer:
            return "github"
        elif "microsoft" in issuer or "azure" in issuer:
            return "microsoft"
        elif "okta" in issuer:
            return "okta"
        elif "auth0" in issuer:
            return "auth0"
        else:
            return issuer.replace("https://", "").replace("http://", "").split("/")[0]

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "OIDCAuth":
        """Create from configuration dictionary."""
        return cls(
            issuer=config["issuer"],
            audience=config["audience"],
            jwks_uri=config.get("jwks_uri"),
            subject_type=config.get("subject_type", "user"),
            subject_id_claim=config.get("subject_id_claim", "sub"),
            zone_id_claim=config.get("zone_id_claim", "org_id"),
            admin_emails=config.get("admin_emails", []),
            allow_default_zone=config.get("allow_default_zone", False),
            require_zone=config.get("require_zone", False),
        )


class MultiOIDCAuth(AuthProvider):
    """Support multiple OIDC providers."""

    def __init__(self, providers: "dict[str, OIDCAuth]") -> None:
        self.providers = providers
        logger.info("Initialized MultiOIDCAuth with providers: %s", list(providers.keys()))

    async def authenticate(self, token: str) -> AuthResult:
        for provider_name, provider in self.providers.items():
            result = await provider.authenticate(token)
            if result.authenticated:
                logger.info("Authenticated via provider: %s", provider_name)
                return result

        logger.debug("Authentication failed for all providers")
        return AuthResult(authenticated=False)

    async def validate_token(self, token: str) -> bool:
        for provider in self.providers.values():
            if await provider.validate_token(token):
                return True
        return False

    def close(self) -> None:
        for provider in self.providers.values():
            provider.close()

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "MultiOIDCAuth":
        """Create from configuration dictionary."""
        providers = {
            name: OIDCAuth.from_config(provider_config)
            for name, provider_config in config.get("providers", {}).items()
        }
        return cls(providers)
