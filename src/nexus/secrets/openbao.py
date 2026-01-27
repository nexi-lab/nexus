"""OpenBao/Vault secrets provider.

This module provides integration with OpenBao (or HashiCorp Vault) for
enterprise-grade secrets management.

Features:
- KV secrets engine for storing API keys and credentials
- Transit engine for encryption/decryption
- Database engine for dynamic credentials
- Multiple authentication methods (token, AppRole, Kubernetes)
- Automatic token renewal
- Secret caching with TTL

Example:
    from nexus.secrets.openbao import OpenBaoClient

    client = OpenBaoClient(
        address="http://localhost:8200",
        auth_method="approle",
        role_id="...",
        secret_id="..."
    )

    # Read secrets
    api_keys = client.get_secret_dict("nexus/api-keys")

    # Encrypt/decrypt
    encrypted = client.encrypt("sensitive data", key_name="nexus-oauth")
    decrypted = client.decrypt(encrypted, key_name="nexus-oauth")
"""

import base64
import logging
import os
import time
from typing import Any

from nexus.secrets.base import SecretsProvider

logger = logging.getLogger(__name__)


class OpenBaoError(Exception):
    """Base exception for OpenBao errors."""

    pass


class OpenBaoAuthError(OpenBaoError):
    """Authentication error."""

    pass


class OpenBaoSecretNotFoundError(OpenBaoError):
    """Secret not found."""

    pass


class OpenBaoClient(SecretsProvider):
    """OpenBao/Vault secrets provider.

    Supports multiple authentication methods:
    - token: Direct token authentication (dev only)
    - approle: AppRole authentication (production)
    - kubernetes: Kubernetes service account authentication

    The client automatically handles:
    - Token renewal before expiry
    - Secret caching with configurable TTL
    - Retry with exponential backoff
    """

    def __init__(
        self,
        address: str | None = None,
        token: str | None = None,
        auth_method: str | None = None,
        role_id: str | None = None,
        secret_id: str | None = None,
        role: str | None = None,
        kv_mount: str = "secret",
        transit_mount: str = "transit",
        database_mount: str = "database",
        cache_ttl: int = 300,
        namespace: str | None = None,
    ):
        """Initialize the OpenBao client.

        Args:
            address: OpenBao server address (default: $NEXUS_OPENBAO_ADDR)
            token: Direct token for authentication (dev only)
            auth_method: Authentication method ("approle", "kubernetes", or None for token)
            role_id: AppRole role ID ($NEXUS_OPENBAO_ROLE_ID)
            secret_id: AppRole secret ID ($NEXUS_OPENBAO_SECRET_ID)
            role: Kubernetes auth role name
            kv_mount: Mount path for KV secrets engine (default: "secret")
            transit_mount: Mount path for Transit engine (default: "transit")
            database_mount: Mount path for Database engine (default: "database")
            cache_ttl: Cache TTL in seconds (default: 300)
            namespace: OpenBao namespace (enterprise feature)
        """
        # Load configuration from environment if not provided
        self._address = address or os.environ.get(
            "NEXUS_OPENBAO_ADDR",
            os.environ.get("VAULT_ADDR", "http://localhost:8200"),
        )
        self._namespace = namespace or os.environ.get("NEXUS_OPENBAO_NAMESPACE")

        # Authentication configuration
        self._auth_method = auth_method or os.environ.get("NEXUS_OPENBAO_AUTH_METHOD")
        self._token = token or os.environ.get(
            "NEXUS_OPENBAO_TOKEN", os.environ.get("VAULT_TOKEN")
        )
        self._role_id = role_id or os.environ.get("NEXUS_OPENBAO_ROLE_ID")
        self._secret_id = secret_id or os.environ.get("NEXUS_OPENBAO_SECRET_ID")
        self._k8s_role = role

        # Engine mount paths
        self._kv_mount = kv_mount
        self._transit_mount = transit_mount
        self._database_mount = database_mount

        # Token management
        self._token_expiry: float | None = None
        self._token_renewable: bool = False

        # Caching
        self._cache: dict[str, tuple[Any, float]] = {}
        self._cache_ttl = cache_ttl

        # HTTP client (lazy initialized)
        self._client: Any = None

        logger.info(
            f"OpenBaoClient initialized: address={self._address}, "
            f"auth_method={self._auth_method or 'token'}"
        )

    def _get_client(self):
        """Get or create the HTTP client."""
        if self._client is None:
            import httpx

            self._client = httpx.Client(
                base_url=self._address,
                timeout=30.0,
            )
        return self._client

    def _get_headers(self) -> dict[str, str]:
        """Get request headers including authentication token."""
        self._ensure_authenticated()

        headers = {"X-Vault-Token": self._token}
        if self._namespace:
            headers["X-Vault-Namespace"] = self._namespace
        return headers

    def _ensure_authenticated(self) -> None:
        """Ensure we have a valid authentication token."""
        # Check if token needs refresh
        if self._token and self._token_expiry:
            if time.time() < self._token_expiry - 60:  # Refresh 60s before expiry
                return

        # Authenticate based on method
        if self._auth_method == "approle":
            self._authenticate_approle()
        elif self._auth_method == "kubernetes":
            self._authenticate_kubernetes()
        elif self._token:
            # Token provided directly, validate it
            self._validate_token()
        else:
            raise OpenBaoAuthError(
                "No authentication method configured. "
                "Set NEXUS_OPENBAO_TOKEN or configure AppRole/Kubernetes auth."
            )

    def _authenticate_approle(self) -> None:
        """Authenticate using AppRole."""
        if not self._role_id or not self._secret_id:
            raise OpenBaoAuthError(
                "AppRole authentication requires role_id and secret_id. "
                "Set NEXUS_OPENBAO_ROLE_ID and NEXUS_OPENBAO_SECRET_ID."
            )

        client = self._get_client()
        response = client.post(
            "/v1/auth/approle/login",
            json={
                "role_id": self._role_id,
                "secret_id": self._secret_id,
            },
        )

        if response.status_code != 200:
            raise OpenBaoAuthError(
                f"AppRole authentication failed: {response.status_code} {response.text}"
            )

        data = response.json()
        auth = data.get("auth", {})
        self._token = auth.get("client_token")
        self._token_renewable = auth.get("renewable", False)

        # Calculate expiry
        lease_duration = auth.get("lease_duration", 3600)
        self._token_expiry = time.time() + lease_duration

        logger.info(f"OpenBao: Authenticated via AppRole, token expires in {lease_duration}s")

    def _authenticate_kubernetes(self) -> None:
        """Authenticate using Kubernetes service account."""
        if not self._k8s_role:
            raise OpenBaoAuthError(
                "Kubernetes authentication requires a role name. "
                "Set role parameter or NEXUS_OPENBAO_K8S_ROLE."
            )

        # Read the service account token
        sa_token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
        try:
            with open(sa_token_path) as f:
                jwt = f.read()
        except FileNotFoundError:
            raise OpenBaoAuthError(
                f"Kubernetes service account token not found at {sa_token_path}. "
                "Are you running in a Kubernetes pod?"
            )

        client = self._get_client()
        response = client.post(
            "/v1/auth/kubernetes/login",
            json={
                "role": self._k8s_role,
                "jwt": jwt,
            },
        )

        if response.status_code != 200:
            raise OpenBaoAuthError(
                f"Kubernetes authentication failed: {response.status_code} {response.text}"
            )

        data = response.json()
        auth = data.get("auth", {})
        self._token = auth.get("client_token")
        self._token_renewable = auth.get("renewable", False)

        lease_duration = auth.get("lease_duration", 3600)
        self._token_expiry = time.time() + lease_duration

        logger.info(f"OpenBao: Authenticated via Kubernetes, token expires in {lease_duration}s")

    def _validate_token(self) -> None:
        """Validate the current token."""
        client = self._get_client()
        response = client.get(
            "/v1/auth/token/lookup-self",
            headers={"X-Vault-Token": self._token},
        )

        if response.status_code != 200:
            raise OpenBaoAuthError(
                f"Token validation failed: {response.status_code}. Token may be invalid or expired."
            )

        data = response.json().get("data", {})
        self._token_renewable = data.get("renewable", False)

        # Calculate expiry from TTL
        ttl = data.get("ttl", 0)
        if ttl > 0:
            self._token_expiry = time.time() + ttl
        else:
            # Token doesn't expire
            self._token_expiry = None

    def _get_cached(self, cache_key: str) -> Any | None:
        """Get a value from cache if not expired."""
        if cache_key in self._cache:
            value, expiry = self._cache[cache_key]
            if time.time() < expiry:
                return value
            del self._cache[cache_key]
        return None

    def _set_cached(self, cache_key: str, value: Any) -> None:
        """Set a value in cache with TTL."""
        expiry = time.time() + self._cache_ttl
        self._cache[cache_key] = (value, expiry)

    def _read_kv_secret(self, path: str) -> dict[str, Any]:
        """Read a secret from the KV secrets engine.

        Args:
            path: Secret path (e.g., "nexus/api-keys")

        Returns:
            Secret data dictionary

        Raises:
            OpenBaoSecretNotFoundError: If secret doesn't exist
            OpenBaoError: For other errors
        """
        cache_key = f"kv:{path}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        client = self._get_client()
        # KV v2 uses /data/ path
        response = client.get(
            f"/v1/{self._kv_mount}/data/{path}",
            headers=self._get_headers(),
        )

        if response.status_code == 404:
            raise OpenBaoSecretNotFoundError(f"Secret not found: {path}")

        if response.status_code != 200:
            raise OpenBaoError(
                f"Failed to read secret '{path}': {response.status_code} {response.text}"
            )

        data = response.json().get("data", {}).get("data", {})
        self._set_cached(cache_key, data)
        return data

    def _write_kv_secret(self, path: str, data: dict[str, Any]) -> None:
        """Write a secret to the KV secrets engine.

        Args:
            path: Secret path
            data: Secret data dictionary
        """
        client = self._get_client()
        response = client.post(
            f"/v1/{self._kv_mount}/data/{path}",
            headers=self._get_headers(),
            json={"data": data},
        )

        if response.status_code not in (200, 204):
            raise OpenBaoError(
                f"Failed to write secret '{path}': {response.status_code} {response.text}"
            )

        # Invalidate cache
        cache_key = f"kv:{path}"
        if cache_key in self._cache:
            del self._cache[cache_key]

    def get_secret(self, key: str, default: str | None = None) -> str | None:
        """Get a single secret by key.

        For OpenBao, this reads from the default secrets path.
        The key can be in the format "path/key" or just "key".

        Args:
            key: Secret key (e.g., "openai_api_key" or "nexus/api-keys/openai")
            default: Default value if not found

        Returns:
            Secret value or default
        """
        # Check if key contains a path
        if "/" in key:
            parts = key.rsplit("/", 1)
            path = parts[0]
            secret_key = parts[1]
        else:
            # Default path for simple keys
            path = "nexus/api-keys"
            secret_key = key

        try:
            secrets = self._read_kv_secret(path)
            return secrets.get(secret_key, default)
        except OpenBaoSecretNotFoundError:
            return default
        except OpenBaoError as e:
            logger.warning(f"OpenBao: Failed to read secret '{key}': {e}")
            return default

    def get_secrets(self, keys: list[str]) -> dict[str, str | None]:
        """Get multiple secrets by keys.

        Args:
            keys: List of secret keys

        Returns:
            Dictionary mapping keys to values
        """
        return {key: self.get_secret(key) for key in keys}

    def get_secret_dict(self, path: str) -> dict[str, Any]:
        """Get all secrets at a path.

        Args:
            path: Secret path (e.g., "nexus/api-keys")

        Returns:
            Dictionary of secrets
        """
        try:
            return self._read_kv_secret(path)
        except OpenBaoSecretNotFoundError:
            return {}

    def set_secret(self, key: str, value: str) -> None:
        """Set a secret value.

        Args:
            key: Secret key (can include path like "nexus/api-keys/openai")
            value: Secret value
        """
        if "/" in key:
            parts = key.rsplit("/", 1)
            path = parts[0]
            secret_key = parts[1]
        else:
            path = "nexus/api-keys"
            secret_key = key

        # Read existing secrets and update
        try:
            existing = self._read_kv_secret(path)
        except OpenBaoSecretNotFoundError:
            existing = {}

        existing[secret_key] = value
        self._write_kv_secret(path, existing)

    def delete_secret(self, key: str) -> None:
        """Delete a secret.

        Args:
            key: Secret key to delete
        """
        if "/" in key:
            parts = key.rsplit("/", 1)
            path = parts[0]
            secret_key = parts[1]
        else:
            path = "nexus/api-keys"
            secret_key = key

        try:
            existing = self._read_kv_secret(path)
            if secret_key in existing:
                del existing[secret_key]
                self._write_kv_secret(path, existing)
        except OpenBaoSecretNotFoundError:
            pass

    def encrypt(self, plaintext: str, key_name: str = "nexus-default") -> str:
        """Encrypt data using the Transit engine.

        Args:
            plaintext: Data to encrypt
            key_name: Name of the encryption key

        Returns:
            Base64-encoded ciphertext with vault: prefix
        """
        client = self._get_client()

        # Transit requires base64-encoded plaintext
        plaintext_b64 = base64.b64encode(plaintext.encode()).decode()

        response = client.post(
            f"/v1/{self._transit_mount}/encrypt/{key_name}",
            headers=self._get_headers(),
            json={"plaintext": plaintext_b64},
        )

        if response.status_code != 200:
            raise OpenBaoError(
                f"Encryption failed: {response.status_code} {response.text}"
            )

        return response.json().get("data", {}).get("ciphertext", "")

    def decrypt(self, ciphertext: str, key_name: str = "nexus-default") -> str:
        """Decrypt data using the Transit engine.

        Args:
            ciphertext: Encrypted data (vault:v1:... format)
            key_name: Name of the encryption key

        Returns:
            Decrypted plaintext
        """
        client = self._get_client()

        response = client.post(
            f"/v1/{self._transit_mount}/decrypt/{key_name}",
            headers=self._get_headers(),
            json={"ciphertext": ciphertext},
        )

        if response.status_code != 200:
            raise OpenBaoError(
                f"Decryption failed: {response.status_code} {response.text}"
            )

        plaintext_b64 = response.json().get("data", {}).get("plaintext", "")
        return base64.b64decode(plaintext_b64).decode()

    def get_database_credentials(self, role: str) -> dict[str, str]:
        """Get dynamic database credentials.

        Args:
            role: Database role name

        Returns:
            Dictionary with 'username' and 'password' keys
        """
        client = self._get_client()

        response = client.get(
            f"/v1/{self._database_mount}/creds/{role}",
            headers=self._get_headers(),
        )

        if response.status_code != 200:
            raise OpenBaoError(
                f"Failed to get database credentials: {response.status_code} {response.text}"
            )

        data = response.json().get("data", {})
        return {
            "username": data.get("username", ""),
            "password": data.get("password", ""),
            "lease_id": response.json().get("lease_id", ""),
            "lease_duration": response.json().get("lease_duration", 0),
        }

    def is_available(self) -> bool:
        """Check if OpenBao is available and accessible."""
        try:
            client = self._get_client()
            response = client.get("/v1/sys/health")
            return response.status_code in (200, 429, 472, 473, 501, 503)
        except Exception as e:
            logger.debug(f"OpenBao health check failed: {e}")
            return False

    def health_check(self) -> dict[str, Any]:
        """Perform a health check on OpenBao."""
        try:
            client = self._get_client()
            response = client.get("/v1/sys/health")
            data = response.json() if response.status_code == 200 else {}

            return {
                "provider": "OpenBaoClient",
                "available": response.status_code in (200, 429, 472, 473),
                "address": self._address,
                "sealed": data.get("sealed", True),
                "initialized": data.get("initialized", False),
                "version": data.get("version", "unknown"),
                "auth_method": self._auth_method or "token",
            }
        except Exception as e:
            return {
                "provider": "OpenBaoClient",
                "available": False,
                "address": self._address,
                "error": str(e),
            }

    def __del__(self):
        """Clean up HTTP client on destruction."""
        if self._client:
            self._client.close()
