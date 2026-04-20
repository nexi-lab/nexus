"""OAuth provider factory (moved from server/auth/oauth_factory.py).

Creates OAuth provider instances from YAML configuration.
"""

import importlib
import logging
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from nexus.bricks.auth.oauth.base_provider import BaseOAuthProvider
from nexus.bricks.auth.oauth.config import OAuthConfig, OAuthProviderConfig
from nexus.contracts.constants import DEFAULT_OAUTH_REDIRECT_URI

logger = logging.getLogger(__name__)


class OAuthProviderFactory:
    """Factory for creating OAuth providers from configuration."""

    def __init__(self, config: OAuthConfig | None = None) -> None:
        if config is not None:
            self._oauth_config = config
        else:
            self._oauth_config = self._get_default_oauth_config()

    @classmethod
    def from_file(cls, path: Path | str) -> "OAuthProviderFactory":
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
        try:
            oauth_config = OAuthConfig(**config_dict)
        except ValidationError as e:
            raise ValueError(f"Invalid OAuth config: {e}") from e

        return cls(config=oauth_config)

    def _get_default_oauth_config(self) -> OAuthConfig:
        oauth_yaml = None
        tried_paths: list[str] = []

        env_path = os.getenv("NEXUS_OAUTH_CONFIG_PATH")
        if env_path:
            tried_paths.append(env_path)
            env_path_obj = Path(env_path)
            if env_path_obj.exists():
                oauth_yaml = env_path_obj

        if not oauth_yaml or not oauth_yaml.exists():
            docker_path = Path("/app/configs/oauth.yaml")
            tried_paths.append(str(docker_path))
            if docker_path.exists():
                oauth_yaml = docker_path

        if not oauth_yaml or not oauth_yaml.exists():
            current_file = Path(__file__)
            configs_dir = current_file.parent.parent.parent.parent.parent.parent / "configs"
            dev_path = configs_dir / "oauth.yaml"
            tried_paths.append(str(dev_path))
            if dev_path.exists():
                oauth_yaml = dev_path

        if not oauth_yaml or not oauth_yaml.exists():
            logger.info(
                "OAuth configuration file not found (tried: %s). "
                "OAuth providers will be empty until configured.",
                ", ".join(tried_paths),
            )
            return OAuthConfig()

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
        if not self._oauth_config:
            return None
        return self._oauth_config.get_provider_config(name)

    def create_provider(
        self,
        name: str,
        redirect_uri: str | None = None,
        scopes: list[str] | None = None,
    ) -> BaseOAuthProvider:
        if not self._oauth_config:
            raise RuntimeError("OAuth configuration not loaded")

        provider_config = self._oauth_config.get_provider_config(name)
        if not provider_config:
            raise ValueError(
                f"OAuth provider '{name}' not found in configuration. "
                f"Available providers: {', '.join(self._oauth_config.get_all_provider_names())}"
            )

        client_id = os.environ.get(provider_config.client_id_env)
        client_secret = os.environ.get(provider_config.client_secret_env)

        if not client_id:
            raise RuntimeError(
                f"OAuth client ID not configured for '{name}'. "
                f"Set {provider_config.client_id_env} environment variable."
            )

        if not provider_config.requires_pkce and not client_secret:
            raise RuntimeError(
                f"OAuth client secret not configured for '{name}'. "
                f"Set {provider_config.client_secret_env} environment variable."
            )

        try:
            module_path, class_name = provider_config.provider_class.rsplit(".", 1)
            module = importlib.import_module(module_path)
            provider_class = getattr(module, class_name)
        except (ImportError, AttributeError) as e:
            raise RuntimeError(
                f"Failed to import OAuth provider class '{provider_config.provider_class}': {e}"
            ) from e

        provider_scopes = scopes if scopes is not None else provider_config.scopes

        if not provider_scopes:
            raise ValueError(
                f"OAuth provider '{name}' requires at least one scope. "
                f"Provide scopes parameter or configure scopes in config."
            )

        provider_redirect_uri = redirect_uri
        if not provider_redirect_uri:
            provider_redirect_uri = provider_config.redirect_uri
            if not provider_redirect_uri:
                provider_redirect_uri = self._oauth_config.redirect_uri
            if not provider_redirect_uri:
                provider_redirect_uri = DEFAULT_OAUTH_REDIRECT_URI

        try:
            provider = provider_class(
                client_id=client_id,
                client_secret=client_secret or "",
                redirect_uri=provider_redirect_uri,
                scopes=provider_scopes,
                provider_name=name,
            )
        except Exception as e:
            raise RuntimeError(f"Failed to instantiate OAuth provider '{name}': {e}") from e

        return provider  # type: ignore[no-any-return]

    def list_providers(self) -> list[OAuthProviderConfig]:
        if not self._oauth_config:
            return []
        return self._oauth_config.providers

    def get_all_provider_names(self) -> list[str]:
        if not self._oauth_config:
            return []
        return self._oauth_config.get_all_provider_names()
