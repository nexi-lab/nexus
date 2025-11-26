"""OAuth provider factory using configuration.

This module provides a factory for creating OAuth providers based on
configuration, allowing providers to be defined in config files rather
than hardcoded.
"""

import importlib
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from nexus.server.auth.oauth_config import OAuthConfig, OAuthProviderConfig
from nexus.server.auth.oauth_provider import OAuthProvider


class OAuthProviderFactory:
    """Factory for creating OAuth providers from configuration."""

    def __init__(self, config: OAuthConfig | None = None):
        """Initialize factory with OAuth config.

        Args:
            config: OAuthConfig instance. If None, uses default OAuth config.
        """
        if config is not None:
            self._oauth_config = config
        else:
            # Use default configuration
            self._oauth_config = self._get_default_oauth_config()

    @classmethod
    def from_file(cls, path: Path | str) -> "OAuthProviderFactory":
        """Create factory from YAML config file.

        Args:
            path: Path to YAML config file

        Returns:
            OAuthProviderFactory instance

        Raises:
            FileNotFoundError: If config file doesn't exist
            ValueError: If config file is invalid
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"OAuth config file not found: {path}")

        with open(path) as f:
            if path.suffix in [".yaml", ".yml"]:
                config_dict = yaml.safe_load(f)
            else:
                raise ValueError(f"Unsupported config file format: {path.suffix}")

        try:
            oauth_config = OAuthConfig(**config_dict)
        except ValidationError as e:
            raise ValueError(f"Invalid OAuth config: {e}") from e

        return cls(config=oauth_config)

    @classmethod
    def from_dict(cls, config_dict: dict[str, Any]) -> "OAuthProviderFactory":
        """Create factory from dictionary.

        Args:
            config_dict: Dictionary containing OAuth config (should have 'providers' key)

        Returns:
            OAuthProviderFactory instance

        Raises:
            ValueError: If config dict is invalid
        """
        try:
            oauth_config = OAuthConfig(**config_dict)
        except ValidationError as e:
            raise ValueError(f"Invalid OAuth config: {e}") from e

        return cls(config=oauth_config)

    def _get_default_oauth_config(self) -> OAuthConfig:
        """Get default OAuth configuration from configs/oauth.yaml.

        Returns:
            OAuthConfig with default provider configurations

        Raises:
            FileNotFoundError: If configs/oauth.yaml doesn't exist
            ValueError: If the config file is invalid or cannot be loaded
        """
        # Load from default oauth.yaml file
        # Try multiple possible locations:
        # 1. Environment variable NEXUS_OAUTH_CONFIG_PATH
        # 2. Docker container path: /app/configs/oauth.yaml
        # 3. Relative to source file (Development: nexus/configs/oauth.yaml)
        import os

        oauth_yaml = None
        tried_paths = []

        # Try environment variable first
        env_path = os.getenv("NEXUS_OAUTH_CONFIG_PATH")
        if env_path:
            tried_paths.append(env_path)
            env_path_obj = Path(env_path)
            if env_path_obj.exists():
                oauth_yaml = env_path_obj

        # Try /app/configs/oauth.yaml (Docker container)
        if not oauth_yaml or not oauth_yaml.exists():
            docker_path = Path("/app/configs/oauth.yaml")
            tried_paths.append(str(docker_path))
            if docker_path.exists():
                oauth_yaml = docker_path

        # Try relative to source file (Development)
        if not oauth_yaml or not oauth_yaml.exists():
            current_file = Path(__file__)
            # Navigate to nexus/configs/oauth.yaml
            # From: nexus/src/nexus/server/auth/oauth_factory.py
            # To: nexus/configs/oauth.yaml
            configs_dir = current_file.parent.parent.parent.parent.parent / "configs"
            dev_path = configs_dir / "oauth.yaml"
            tried_paths.append(str(dev_path))
            if dev_path.exists():
                oauth_yaml = dev_path

        if not oauth_yaml or not oauth_yaml.exists():
            paths_tried = "\n  - ".join(tried_paths)
            raise FileNotFoundError(
                f"OAuth configuration file not found. Tried:\n  - {paths_tried}\n"
                f"Please create the file or provide an OAuthConfig instance to OAuthProviderFactory."
            )

        try:
            with open(oauth_yaml) as f:
                config_dict = yaml.safe_load(f)
                if not config_dict:
                    raise ValueError(f"OAuth configuration file is empty: {oauth_yaml}")
                return OAuthConfig(**config_dict)
        except ValidationError as e:
            raise ValueError(f"Invalid OAuth configuration in {oauth_yaml}: {e}") from e
        except yaml.YAMLError as e:
            raise ValueError(f"Failed to parse YAML in {oauth_yaml}: {e}") from e

    def get_provider_config(self, name: str) -> OAuthProviderConfig | None:
        """Get provider configuration by name.

        Args:
            name: Provider name/identifier

        Returns:
            OAuthProviderConfig if found, None otherwise
        """
        if not self._oauth_config:
            return None
        return self._oauth_config.get_provider_config(name)

    def create_provider(
        self,
        name: str,
        redirect_uri: str | None = None,
        scopes: list[str] | None = None,
    ) -> OAuthProvider:
        """Create an OAuth provider instance from configuration.

        Args:
            name: Provider name/identifier (e.g., 'google-drive', 'microsoft', 'x')
            redirect_uri: OAuth redirect URI (optional, uses config default if not provided)
            scopes: Custom scopes to use (optional, uses scopes from config if not provided)

        Returns:
            OAuthProvider instance

        Raises:
            ValueError: If provider not found in config, credentials not configured, or scopes empty
            RuntimeError: If provider class cannot be imported or instantiated
        """
        if not self._oauth_config:
            raise RuntimeError("OAuth configuration not loaded")

        provider_config = self._oauth_config.get_provider_config(name)
        if not provider_config:
            raise ValueError(
                f"OAuth provider '{name}' not found in configuration. "
                f"Available providers: {', '.join(self._oauth_config.get_all_provider_names())}"
            )

        # Get credentials from environment
        client_id = os.environ.get(provider_config.client_id_env)
        client_secret = os.environ.get(provider_config.client_secret_env)

        if not client_id:
            raise RuntimeError(
                f"OAuth client ID not configured for '{name}'. "
                f"Set {provider_config.client_id_env} environment variable."
            )

        # Client secret is optional for PKCE providers
        if not provider_config.requires_pkce and not client_secret:
            raise RuntimeError(
                f"OAuth client secret not configured for '{name}'. "
                f"Set {provider_config.client_secret_env} environment variable."
            )

        # Import provider class
        try:
            module_path, class_name = provider_config.provider_class.rsplit(".", 1)
            module = importlib.import_module(module_path)
            provider_class = getattr(module, class_name)
        except (ImportError, AttributeError) as e:
            raise RuntimeError(
                f"Failed to import OAuth provider class '{provider_config.provider_class}': {e}"
            ) from e

        # Use scopes from config if not provided
        provider_scopes = scopes if scopes is not None else provider_config.scopes

        # Validate scopes are not empty
        if not provider_scopes:
            raise ValueError(
                f"OAuth provider '{name}' requires at least one scope. "
                f"Provide scopes parameter or configure scopes in config."
            )

        # Resolve redirect_uri: parameter > provider config > global config > default
        provider_redirect_uri = redirect_uri
        if not provider_redirect_uri:
            # Use redirect_uri from provider config if available
            provider_redirect_uri = provider_config.redirect_uri

            # Fall back to global redirect_uri from OAuthConfig if provider doesn't have one
            if not provider_redirect_uri:
                provider_redirect_uri = self._oauth_config.redirect_uri

            # Final fallback to hardcoded default
            if not provider_redirect_uri:
                provider_redirect_uri = "http://localhost:3000/oauth/callback"

        # Instantiate provider
        try:
            provider = provider_class(
                client_id=client_id,
                client_secret=client_secret or "",
                redirect_uri=provider_redirect_uri,
                scopes=provider_scopes,
                provider_name=name,  # Use config name as provider_name
            )
        except Exception as e:
            raise RuntimeError(f"Failed to instantiate OAuth provider '{name}': {e}") from e

        return provider  # type: ignore[no-any-return]

    def list_providers(self) -> list[OAuthProviderConfig]:
        """List all configured OAuth providers.

        Returns:
            List of OAuthProviderConfig instances
        """
        if not self._oauth_config:
            return []
        return self._oauth_config.providers

    def get_all_provider_names(self) -> list[str]:
        """Get list of all configured provider names.

        Returns:
            List of provider names
        """
        if not self._oauth_config:
            return []
        return self._oauth_config.get_all_provider_names()
