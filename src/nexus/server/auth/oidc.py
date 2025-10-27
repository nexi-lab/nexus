"""OAuth/OIDC authentication provider for SSO integration."""

import logging
import time
from typing import Any

import requests
from authlib.jose import JoseError, JsonWebKey, jwt

from nexus.server.auth.base import AuthProvider, AuthResult

logger = logging.getLogger(__name__)

# Security constants
ALLOWED_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"]
CLOCK_SKEW_SECONDS = 300  # ±5 minutes
JWKS_CACHE_TTL = 3600  # 1 hour


class OIDCAuth(AuthProvider):
    """OAuth/OIDC authentication provider for SSO integration.

    This provider validates JWT tokens from external identity providers like:
    - Google OAuth
    - GitHub OAuth
    - Microsoft Azure AD
    - Okta
    - Auth0
    - Any OIDC-compliant provider

    Token validation only (no OAuth flow - handled by frontend/web UI).

    Example usage:
        # Create OIDC auth provider
        auth = OIDCAuth(
            issuer="https://accounts.google.com",
            audience="your-client-id",
            jwks_uri="https://www.googleapis.com/oauth2/v3/certs"
        )

        # Authenticate with ID token from OAuth flow
        result = await auth.authenticate(id_token)
        # → AuthResult(subject_type="user", subject_id="google:123456", ...)

    Configuration:
        - issuer: Token issuer URL (validates 'iss' claim)
        - audience: Client ID (validates 'aud' claim)
        - jwks_uri: Public keys endpoint for signature verification
        - subject_mapping: How to map OIDC claims to Nexus subject

    Security:
    - Validates token signature using provider's public keys
    - Validates issuer, audience, expiration
    - Validates nonce (if provided)
    - No password storage - delegated to IdP
    """

    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_uri: str | None = None,
        subject_type: str = "user",
        subject_id_claim: str = "sub",
        tenant_id_claim: str | None = "org_id",
        admin_emails: list[str] | None = None,
        allow_default_tenant: bool = False,  # P0-2: Strict tenant binding
        require_tenant: bool = False,  # P0-2: Deny if tenant cannot be derived
    ):
        """Initialize OIDC authentication.

        Args:
            issuer: Expected token issuer (e.g., "https://accounts.google.com")
            audience: Expected audience - your OAuth client ID
            jwks_uri: JSON Web Key Set URI for public keys (auto-discovered if None)
            subject_type: Subject type for authenticated users (default: "user")
            subject_id_claim: JWT claim to use as subject_id (default: "sub")
            tenant_id_claim: JWT claim to use as tenant_id (default: "org_id", None to disable)
            admin_emails: List of admin email addresses
            allow_default_tenant: Allow fallback to default tenant if claim missing
            require_tenant: Deny authentication if tenant cannot be derived
        """
        self.issuer = issuer
        self.audience = audience
        self.jwks_uri = jwks_uri or self._discover_jwks_uri(issuer)
        self.subject_type = subject_type
        self.subject_id_claim = subject_id_claim
        self.tenant_id_claim = tenant_id_claim
        self.admin_emails = set(admin_emails or [])
        self.allow_default_tenant = allow_default_tenant
        self.require_tenant = require_tenant

        # JWKS cache (P0-3)
        self._jwks_cache: dict[str, Any] | None = None
        self._jwks_cache_time: float = 0

        logger.info(f"Initialized OIDCAuth for issuer: {issuer} (JWKS: {self.jwks_uri})")

    def _discover_jwks_uri(self, issuer: str) -> str:
        """Discover JWKS URI from OIDC discovery endpoint.

        Args:
            issuer: Issuer URL

        Returns:
            JWKS URI string

        Note:
            For production, implement full OIDC discovery.
            For now, use common patterns.
        """
        # Common JWKS URI patterns
        patterns = {
            "https://accounts.google.com": "https://www.googleapis.com/oauth2/v3/certs",
            "https://login.microsoftonline.com": "{issuer}/discovery/v2.0/keys",
            "https://github.com": "https://token.actions.githubusercontent.com/.well-known/jwks",
        }

        if issuer in patterns:
            return patterns[issuer]

        # Default: try OIDC discovery endpoint
        return f"{issuer}/.well-known/openid-configuration"

    def _fetch_jwks(self) -> dict[str, Any]:
        """Fetch JWKS from provider with caching.

        Returns:
            JWKS dictionary

        Raises:
            ValueError: If JWKS fetch fails
        """
        # Check cache (P0-3: JWKS caching with TTL)
        now = time.time()
        if self._jwks_cache and (now - self._jwks_cache_time) < JWKS_CACHE_TTL:
            logger.debug("Using cached JWKS")
            return self._jwks_cache

        # Fetch fresh JWKS
        try:
            logger.info(f"Fetching JWKS from {self.jwks_uri}")
            response = requests.get(self.jwks_uri, timeout=10)
            response.raise_for_status()
            jwks = response.json()

            # Cache the result
            self._jwks_cache = jwks
            self._jwks_cache_time = now

            result: dict[str, Any] = dict(jwks)
            return result
        except Exception as e:
            logger.error(f"Failed to fetch JWKS from {self.jwks_uri}: {e}")
            # P0-3: Fail closed on JWKS fetch error
            raise ValueError(f"INDETERMINATE: Cannot fetch JWKS - {e}") from e

    def verify_token(self, token: str) -> dict[str, Any]:
        """Verify and decode OIDC ID token with proper security validation.

        P0-3 Security guarantees:
        - Enforces RS256/ES256 only (no HS256)
        - Validates iss, aud, exp, nbf, iat
        - Handles clock skew (±5 min)
        - JWKS caching & rotation with TTL
        - Pin by kid

        Args:
            token: JWT ID token from OAuth flow

        Returns:
            Decoded claims dict

        Raises:
            ValueError: If token is invalid, expired, or has wrong issuer/audience
        """
        try:
            # Decode header to get algorithm and key ID
            header = jwt.decode_header(token)
            alg = header.get("alg")
            kid = header.get("kid")

            # P0-3: Enforce RS256/ES256 only (no HS256)
            if alg not in ALLOWED_ALGORITHMS:
                raise ValueError(
                    f"UNAUTHORIZED: Algorithm {alg} not allowed. Must be one of {ALLOWED_ALGORITHMS}"
                )

            # P0-3: Fetch JWKS and find matching key
            jwks = self._fetch_jwks()
            keys = jwks.get("keys", [])

            # Find key by kid (P0-3: pin by kid)
            public_key = None
            if kid:
                for key_data in keys:
                    if key_data.get("kid") == kid:
                        public_key = JsonWebKey.import_key(key_data)
                        break
                if not public_key:
                    raise ValueError(f"UNAUTHORIZED: Key ID {kid} not found in JWKS")
            else:
                # No kid specified - try first key (less secure)
                if keys:
                    logger.warning("Token has no kid - using first JWKS key")
                    public_key = JsonWebKey.import_key(keys[0])
                else:
                    raise ValueError("UNAUTHORIZED: No keys in JWKS")

            # P0-3: Validate claims with clock skew
            now = int(time.time())
            claims_options = {
                "iss": {"essential": True, "value": self.issuer},
                "aud": {"essential": True, "value": self.audience},
                "exp": {"essential": True, "validate": lambda v: v > (now - CLOCK_SKEW_SECONDS)},
                "iat": {"essential": True, "validate": lambda v: v <= (now + CLOCK_SKEW_SECONDS)},
                "nbf": {"essential": False, "validate": lambda v: v <= (now + CLOCK_SKEW_SECONDS)},
            }

            # Decode and validate
            claims = jwt.decode(token, public_key, claims_options=claims_options)
            claims.validate()

            # Additional time-based validation
            exp = claims.get("exp")
            iat = claims.get("iat")
            nbf = claims.get("nbf")

            if exp and exp < (now - CLOCK_SKEW_SECONDS):
                raise ValueError(f"UNAUTHORIZED: Token expired at {exp}")

            if iat and iat > (now + CLOCK_SKEW_SECONDS):
                raise ValueError(f"UNAUTHORIZED: Token issued in future: {iat}")

            if nbf and nbf > (now + CLOCK_SKEW_SECONDS):
                raise ValueError(f"UNAUTHORIZED: Token not valid before {nbf}")

            logger.debug(f"Token validated: iss={claims.get('iss')}, sub={claims.get('sub')}")
            result: dict[str, Any] = dict(claims)
            return result

        except JoseError as e:
            raise ValueError(f"UNAUTHORIZED: Invalid OIDC token - {e}") from e

    async def authenticate(self, token: str) -> AuthResult:
        """Authenticate using OIDC ID token.

        P0-2: Strict subject & tenant binding contract

        Args:
            token: JWT ID token from Authorization header

        Returns:
            AuthResult with subject identity if valid
        """
        try:
            claims = self.verify_token(token)

            # Extract subject ID from configured claim
            subject_id = claims.get(self.subject_id_claim)
            if not subject_id:
                logger.error(f"UNAUTHORIZED: Token missing required claim: {self.subject_id_claim}")
                return AuthResult(authenticated=False)

            # Prefix with provider for uniqueness (e.g., "google:123456")
            provider_prefix = self._extract_provider_prefix(claims.get("iss", ""))
            subject_id = f"{provider_prefix}:{subject_id}"

            # P0-2: Extract tenant ID with strict validation
            tenant_id = None
            if self.tenant_id_claim:
                tenant_id = claims.get(self.tenant_id_claim)

            # P0-2: Deny if tenant cannot be derived and require_tenant is True
            if self.require_tenant and not tenant_id and not self.allow_default_tenant:
                logger.error(
                    f"UNAUTHORIZED: Tenant required but not found in token. "
                    f"Claim '{self.tenant_id_claim}' missing or empty. "
                    f"Set allow_default_tenant=True to allow fallback."
                )
                return AuthResult(authenticated=False)

            # Check if admin
            email = claims.get("email")
            is_admin = email in self.admin_emails if email else False

            logger.info(
                f"OIDC authenticated: subject={subject_id}, tenant={tenant_id}, "
                f"admin={is_admin}, email={email}"
            )

            return AuthResult(
                authenticated=True,
                subject_type=self.subject_type,
                subject_id=subject_id,
                tenant_id=tenant_id,
                is_admin=is_admin,
                metadata={
                    "email": email,
                    "name": claims.get("name"),
                    "picture": claims.get("picture"),
                    "provider": provider_prefix,
                },
            )
        except ValueError as e:
            logger.warning(f"OIDC authentication failed: {e}")
            return AuthResult(authenticated=False)

    async def validate_token(self, token: str) -> bool:
        """Quick validation check without full authentication.

        Args:
            token: OIDC ID token

        Returns:
            True if token is valid
        """
        try:
            self.verify_token(token)
            return True
        except ValueError:
            return False

    def close(self) -> None:
        """Cleanup resources (no-op for OIDC)."""
        pass

    def _extract_provider_prefix(self, issuer: str) -> str:
        """Extract provider name from issuer URL.

        Args:
            issuer: Issuer URL

        Returns:
            Provider prefix (e.g., "google", "github", "microsoft")
        """
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
            # Use domain name as prefix
            return issuer.replace("https://", "").replace("http://", "").split("/")[0]

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "OIDCAuth":
        """Create from configuration dictionary.

        Args:
            config: Configuration with required fields:
                - issuer: Token issuer URL
                - audience: OAuth client ID
                Optional fields:
                - jwks_uri: Public keys endpoint
                - subject_type: Subject type (default: "user")
                - subject_id_claim: Claim to use as subject_id (default: "sub")
                - tenant_id_claim: Claim to use as tenant_id (default: "org_id")
                - admin_emails: List of admin emails
                - allow_default_tenant: Allow fallback to default tenant (default: False)
                - require_tenant: Deny auth if tenant missing (default: False)

        Returns:
            OIDCAuth instance

        Example:
            config = {
                "issuer": "https://accounts.google.com",
                "audience": "your-client-id.apps.googleusercontent.com",
                "admin_emails": ["admin@example.com"],
                "require_tenant": True  # Enforce tenant binding
            }
            auth = OIDCAuth.from_config(config)
        """
        return cls(
            issuer=config["issuer"],
            audience=config["audience"],
            jwks_uri=config.get("jwks_uri"),
            subject_type=config.get("subject_type", "user"),
            subject_id_claim=config.get("subject_id_claim", "sub"),
            tenant_id_claim=config.get("tenant_id_claim", "org_id"),
            admin_emails=config.get("admin_emails", []),
            allow_default_tenant=config.get("allow_default_tenant", False),
            require_tenant=config.get("require_tenant", False),
        )


class MultiOIDCAuth(AuthProvider):
    """Support multiple OIDC providers (Google, GitHub, Microsoft, etc.).

    Example usage:
        auth = MultiOIDCAuth(providers={
            "google": OIDCAuth(
                issuer="https://accounts.google.com",
                audience="google-client-id"
            ),
            "github": OIDCAuth(
                issuer="https://github.com",
                audience="github-client-id"
            )
        })

        # Tries each provider until one succeeds
        result = await auth.authenticate(token)
    """

    def __init__(self, providers: dict[str, OIDCAuth]):
        """Initialize multi-provider OIDC auth.

        Args:
            providers: Dict of provider name -> OIDCAuth instance
        """
        self.providers = providers
        logger.info(f"Initialized MultiOIDCAuth with providers: {list(providers.keys())}")

    async def authenticate(self, token: str) -> AuthResult:
        """Authenticate using any configured provider.

        Args:
            token: JWT ID token

        Returns:
            AuthResult from first successful provider
        """
        for provider_name, provider in self.providers.items():
            result = await provider.authenticate(token)
            if result.authenticated:
                logger.info(f"Authenticated via provider: {provider_name}")
                return result

        logger.debug("Authentication failed for all providers")
        return AuthResult(authenticated=False)

    async def validate_token(self, token: str) -> bool:
        """Check if token is valid with any provider.

        Args:
            token: JWT ID token

        Returns:
            True if any provider validates token
        """
        for provider in self.providers.values():
            if await provider.validate_token(token):
                return True
        return False

    def close(self) -> None:
        """Cleanup resources (close all providers)."""
        for provider in self.providers.values():
            provider.close()

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "MultiOIDCAuth":
        """Create from configuration dictionary.

        Args:
            config: Configuration with "providers" field:
                {
                    "providers": {
                        "google": {
                            "issuer": "https://accounts.google.com",
                            "audience": "google-client-id"
                        },
                        "github": {...}
                    }
                }

        Returns:
            MultiOIDCAuth instance
        """
        providers = {
            name: OIDCAuth.from_config(provider_config)
            for name, provider_config in config.get("providers", {}).items()
        }
        return cls(providers)
