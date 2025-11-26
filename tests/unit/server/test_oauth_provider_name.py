"""Tests for OAuth provider name requirement and configuration."""

from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
import yaml

from nexus.server.auth.google_oauth import GoogleOAuthProvider
from nexus.server.auth.microsoft_oauth import MicrosoftOAuthProvider
from nexus.server.auth.oauth_config import OAuthConfig, OAuthProviderConfig
from nexus.server.auth.oauth_factory import OAuthProviderFactory
from nexus.server.auth.oauth_provider import OAuthError
from nexus.server.auth.x_oauth import XOAuthProvider


class TestOAuthProviderNameRequirement:
    """Test that provider_name is required and cannot be None or empty."""

    def test_provider_name_required(self):
        """Test that provider_name is required parameter."""
        # This should fail because provider_name is required
        with pytest.raises(TypeError):
            GoogleOAuthProvider(
                client_id="test-id",
                client_secret="secret",
                redirect_uri="http://localhost/callback",
                scopes=["https://www.googleapis.com/auth/drive"],
            )

    def test_provider_name_cannot_be_empty(self):
        """Test that empty provider_name raises error."""
        with pytest.raises(OAuthError, match="provider_name is required"):
            GoogleOAuthProvider(
                client_id="test-id",
                client_secret="secret",
                redirect_uri="http://localhost/callback",
                scopes=["https://www.googleapis.com/auth/drive"],
                provider_name="",
            )

    def test_provider_name_cannot_be_none(self):
        """Test that None provider_name raises error."""
        # Since provider_name is required, passing None will be caught by validation
        with pytest.raises(OAuthError, match="provider_name is required"):
            GoogleOAuthProvider(
                client_id="test-id",
                client_secret="secret",
                redirect_uri="http://localhost/callback",
                scopes=["https://www.googleapis.com/auth/drive"],
                provider_name=None,  # type: ignore[arg-type]
            )

    def test_provider_name_stored_correctly(self):
        """Test that provider_name is stored correctly."""
        provider = GoogleOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["https://www.googleapis.com/auth/drive"],
            provider_name="google-drive",
        )

        assert provider.provider_name == "google-drive"

    def test_microsoft_provider_name_required(self):
        """Test Microsoft provider requires provider_name."""
        with pytest.raises(TypeError):
            MicrosoftOAuthProvider(
                client_id="test-id",
                client_secret="secret",
                redirect_uri="http://localhost/callback",
                scopes=["Files.ReadWrite.All"],
            )

    def test_x_provider_name_required(self):
        """Test X provider requires provider_name."""
        with pytest.raises(TypeError):
            XOAuthProvider(
                client_id="test-id",
                redirect_uri="http://localhost/callback",
                scopes=["tweet.read"],
            )


class TestOAuthProviderFactoryWithProviderName:
    """Test OAuthProviderFactory creates providers with provider_name from config."""

    def test_factory_sets_provider_name_from_config(self):
        """Test that factory sets provider_name from config name."""
        config = OAuthConfig(
            providers=[
                OAuthProviderConfig(
                    name="google-drive",
                    display_name="Google Drive",
                    provider_class="nexus.server.auth.google_oauth.GoogleOAuthProvider",
                    scopes=["https://www.googleapis.com/auth/drive"],
                    client_id_env="TEST_GOOGLE_CLIENT_ID",
                    client_secret_env="TEST_GOOGLE_CLIENT_SECRET",
                )
            ]
        )

        factory = OAuthProviderFactory(config=config)

        with patch.dict(
            "os.environ",
            {
                "TEST_GOOGLE_CLIENT_ID": "test-client-id",
                "TEST_GOOGLE_CLIENT_SECRET": "test-secret",
            },
        ):
            provider = factory.create_provider(
                name="google-drive",
                redirect_uri="http://localhost/callback",
            )

            assert provider.provider_name == "google-drive"
            assert isinstance(provider, GoogleOAuthProvider)

    def test_factory_sets_provider_name_for_microsoft(self):
        """Test that factory sets provider_name for Microsoft provider."""
        config = OAuthConfig(
            providers=[
                OAuthProviderConfig(
                    name="microsoft-onedrive",
                    display_name="Microsoft OneDrive",
                    provider_class="nexus.server.auth.microsoft_oauth.MicrosoftOAuthProvider",
                    scopes=["Files.ReadWrite.All"],
                    client_id_env="TEST_MICROSOFT_CLIENT_ID",
                    client_secret_env="TEST_MICROSOFT_CLIENT_SECRET",
                )
            ]
        )

        factory = OAuthProviderFactory(config=config)

        with patch.dict(
            "os.environ",
            {
                "TEST_MICROSOFT_CLIENT_ID": "test-client-id",
                "TEST_MICROSOFT_CLIENT_SECRET": "test-secret",
            },
        ):
            provider = factory.create_provider(
                name="microsoft-onedrive",
                redirect_uri="http://localhost/callback",
            )

            assert provider.provider_name == "microsoft-onedrive"
            assert isinstance(provider, MicrosoftOAuthProvider)

    def test_factory_sets_provider_name_for_x(self):
        """Test that factory sets provider_name for X provider."""
        config = OAuthConfig(
            providers=[
                OAuthProviderConfig(
                    name="x",
                    display_name="X (Twitter)",
                    provider_class="nexus.server.auth.x_oauth.XOAuthProvider",
                    scopes=["tweet.read"],
                    client_id_env="TEST_X_CLIENT_ID",
                    client_secret_env="TEST_X_CLIENT_SECRET",
                    requires_pkce=True,
                )
            ]
        )

        factory = OAuthProviderFactory(config=config)

        with patch.dict(
            "os.environ",
            {
                "TEST_X_CLIENT_ID": "test-client-id",
            },
        ):
            provider = factory.create_provider(
                name="x",
                redirect_uri="http://localhost/callback",
            )

            assert provider.provider_name == "x"
            assert isinstance(provider, XOAuthProvider)


class TestProviderUsesProviderNameInCredentials:
    """Test that providers use provider_name in OAuth credentials."""

    @pytest.mark.asyncio
    async def test_google_provider_uses_provider_name_in_credential(self):
        """Test Google provider uses provider_name in exchange_code credential."""
        provider = GoogleOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["https://www.googleapis.com/auth/drive"],
            provider_name="google-drive",
        )

        mock_response = Mock()
        mock_response.json.return_value = {
            "access_token": "ya29.test-token",
            "refresh_token": "1//test-refresh",
            "expires_in": 3600,
            "scope": "https://www.googleapis.com/auth/drive",
            "token_type": "Bearer",
        }
        mock_response.raise_for_status = Mock()

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )

            credential = await provider.exchange_code("test-code")

            assert credential.provider == "google-drive"
            assert credential.access_token == "ya29.test-token"

    @pytest.mark.asyncio
    async def test_microsoft_provider_uses_provider_name_in_credential(self):
        """Test Microsoft provider uses provider_name in exchange_code credential."""
        provider = MicrosoftOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["Files.ReadWrite.All"],
            provider_name="microsoft-onedrive",
        )

        mock_response = Mock()
        mock_response.json.return_value = {
            "access_token": "EwAoA8l6BAAU...",
            "refresh_token": "M.R3_BAY.-CfvKc...",
            "expires_in": 3600,
            "scope": "Files.ReadWrite.All offline_access",
            "token_type": "Bearer",
        }
        mock_response.raise_for_status = Mock()

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )

            credential = await provider.exchange_code("test-code")

            assert credential.provider == "microsoft-onedrive"
            assert credential.access_token == "EwAoA8l6BAAU..."

    def test_google_provider_uses_provider_name_in_parse_token_response(self):
        """Test Google provider uses provider_name in _parse_token_response."""
        provider = GoogleOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["https://www.googleapis.com/auth/drive"],
            provider_name="gmail",
        )

        token_data = {
            "access_token": "ya29.test",
            "refresh_token": "1//test",
            "expires_in": 3600,
            "scope": "https://www.googleapis.com/auth/gmail.readonly",
            "token_type": "Bearer",
        }

        credential = provider._parse_token_response(token_data)

        assert credential.provider == "gmail"
        assert credential.access_token == "ya29.test"

    def test_microsoft_provider_uses_provider_name_in_parse_token_response(self):
        """Test Microsoft provider uses provider_name in _parse_token_response."""
        provider = MicrosoftOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["Files.ReadWrite.All"],
            provider_name="microsoft-onedrive",
        )

        token_data = {
            "access_token": "EwAoA8l6BAAU...",
            "refresh_token": "M.R3_BAY.-CfvKc...",
            "expires_in": 3600,
            "scope": "Files.ReadWrite.All offline_access",
            "token_type": "Bearer",
        }

        credential = provider._parse_token_response(token_data)

        assert credential.provider == "microsoft-onedrive"
        assert credential.access_token == "EwAoA8l6BAAU..."


class TestOAuthFactoryDefaultConfig:
    """Test OAuthProviderFactory loading default config from oauth.yaml."""

    def test_get_default_oauth_config_loads_from_file(self, tmp_path):
        """Test that default config loads from oauth.yaml if it exists."""
        # Create a temporary oauth.yaml file
        config_dir = tmp_path / "configs"
        config_dir.mkdir()
        oauth_yaml = config_dir / "oauth.yaml"

        config_data = {
            "providers": [
                {
                    "name": "test-provider",
                    "display_name": "Test Provider",
                    "provider_class": "nexus.server.auth.google_oauth.GoogleOAuthProvider",
                    "scopes": ["https://www.googleapis.com/auth/drive"],
                    "client_id_env": "TEST_CLIENT_ID",
                    "client_secret_env": "TEST_CLIENT_SECRET",
                }
            ]
        }

        with open(oauth_yaml, "w") as f:
            yaml.dump(config_data, f)

        factory = OAuthProviderFactory()

        # Mock Path(__file__) to return a path that resolves to our temp directory
        with patch(
            "nexus.server.auth.oauth_factory.__file__",
            str(tmp_path / "src" / "nexus" / "server" / "auth" / "oauth_factory.py"),
        ):
            # Call _get_default_oauth_config
            config = factory._get_default_oauth_config()

            assert len(config.providers) == 1
            assert config.providers[0].name == "test-provider"

    def test_get_default_oauth_config_raises_if_file_not_found(self, tmp_path):
        """Test that default config raises error if oauth.yaml doesn't exist."""
        factory = OAuthProviderFactory()

        # Mock __file__ to point to a non-existent directory
        with (
            patch(
                "nexus.server.auth.oauth_factory.__file__",
                str(tmp_path / "nonexistent" / "oauth_factory.py"),
            ),
            pytest.raises(FileNotFoundError, match="OAuth configuration file not found"),
        ):
            factory._get_default_oauth_config()

    def test_get_default_oauth_config_raises_if_file_empty(self, tmp_path):
        """Test that default config raises error if oauth.yaml is empty."""
        config_dir = tmp_path / "configs"
        config_dir.mkdir()
        oauth_yaml = config_dir / "oauth.yaml"
        oauth_yaml.write_text("")  # Empty file

        factory = OAuthProviderFactory()

        # Mock __file__ to point to our temp directory
        with (
            patch(
                "nexus.server.auth.oauth_factory.__file__",
                str(tmp_path / "src" / "nexus" / "server" / "auth" / "oauth_factory.py"),
            ),
            pytest.raises(ValueError, match="OAuth configuration file is empty"),
        ):
            factory._get_default_oauth_config()

    def test_get_default_oauth_config_raises_if_invalid_yaml(self, tmp_path):
        """Test that default config raises error if oauth.yaml is invalid."""
        config_dir = tmp_path / "configs"
        config_dir.mkdir()
        oauth_yaml = config_dir / "oauth.yaml"
        oauth_yaml.write_text("invalid: yaml: content: [")  # Invalid YAML

        factory = OAuthProviderFactory()

        # Mock __file__ to point to our temp directory
        with (
            patch(
                "nexus.server.auth.oauth_factory.__file__",
                str(tmp_path / "src" / "nexus" / "server" / "auth" / "oauth_factory.py"),
            ),
            pytest.raises(ValueError, match="Failed to parse YAML"),
        ):
            factory._get_default_oauth_config()

    def test_factory_from_file(self, tmp_path):
        """Test creating factory from YAML file."""
        oauth_yaml = tmp_path / "oauth.yaml"

        config_data = {
            "providers": [
                {
                    "name": "test-provider",
                    "display_name": "Test Provider",
                    "provider_class": "nexus.server.auth.google_oauth.GoogleOAuthProvider",
                    "scopes": ["https://www.googleapis.com/auth/drive"],
                    "client_id_env": "TEST_CLIENT_ID",
                    "client_secret_env": "TEST_CLIENT_SECRET",
                }
            ]
        }

        with open(oauth_yaml, "w") as f:
            yaml.dump(config_data, f)

        factory = OAuthProviderFactory.from_file(oauth_yaml)

        assert len(factory.list_providers()) == 1
        assert factory.list_providers()[0].name == "test-provider"

    def test_factory_from_dict(self):
        """Test creating factory from dictionary."""
        config_dict = {
            "providers": [
                {
                    "name": "test-provider",
                    "display_name": "Test Provider",
                    "provider_class": "nexus.server.auth.google_oauth.GoogleOAuthProvider",
                    "scopes": ["https://www.googleapis.com/auth/drive"],
                    "client_id_env": "TEST_CLIENT_ID",
                    "client_secret_env": "TEST_CLIENT_SECRET",
                }
            ]
        }

        factory = OAuthProviderFactory.from_dict(config_dict)

        assert len(factory.list_providers()) == 1
        assert factory.list_providers()[0].name == "test-provider"


class TestProviderNameInRefreshToken:
    """Test that provider_name is preserved in refresh_token operations."""

    @pytest.mark.asyncio
    async def test_refresh_token_preserves_provider_name(self):
        """Test that refresh_token preserves provider_name from original credential."""
        provider = GoogleOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["https://www.googleapis.com/auth/drive"],
            provider_name="google-drive",
        )

        from datetime import UTC, datetime, timedelta

        from nexus.server.auth.oauth_provider import OAuthCredential

        old_credential = OAuthCredential(
            access_token="old-token",
            refresh_token="refresh-token",
            token_type="Bearer",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
            provider="google-drive",
            user_email="test@example.com",
            scopes=["https://www.googleapis.com/auth/drive"],
        )

        mock_response = Mock()
        mock_response.json.return_value = {
            "access_token": "new-token",
            "expires_in": 3600,
            "token_type": "Bearer",
        }
        mock_response.raise_for_status = Mock()

        from unittest.mock import AsyncMock

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )

            new_credential = await provider.refresh_token(old_credential)

            assert new_credential.provider == "google-drive"
            assert new_credential.access_token == "new-token"
            assert new_credential.user_email == "test@example.com"


class TestLoadOAuthYaml:
    """Test loading and validating the actual oauth.yaml configuration file."""

    def test_load_oauth_yaml_from_configs_directory(self):
        """Test that the actual oauth.yaml file can be loaded and all providers are valid."""

        # Get the path to the actual oauth.yaml file
        # From tests/unit/server/test_oauth_provider_name.py
        # to nexus/configs/oauth.yaml
        test_file = Path(__file__)
        # tests/unit/server/test_oauth_provider_name.py -> tests/unit/server -> tests/unit -> tests -> nexus -> configs
        nexus_root = test_file.parent.parent.parent.parent
        oauth_yaml = nexus_root / "configs" / "oauth.yaml"

        # Raise error if file doesn't exist - this is a required configuration file
        if not oauth_yaml.exists():
            raise FileNotFoundError(
                f"oauth.yaml not found at {oauth_yaml}. "
                f"This file is required for OAuth provider configuration."
            )

        # Load the factory from the actual file
        factory = OAuthProviderFactory.from_file(oauth_yaml)

        # Get all providers
        providers = factory.list_providers()

        # Validate that we have providers
        assert len(providers) > 0, "No providers found in oauth.yaml"

        # Expected providers from oauth.yaml
        expected_providers = {
            "google-drive",
            "gmail",
            "google-cloud-storage",
            "microsoft-onedrive",
            "x",
        }

        # Get actual provider names
        actual_provider_names = {p.name for p in providers}

        # Check that all expected providers are present
        assert expected_providers.issubset(actual_provider_names), (
            f"Missing expected providers. Expected: {expected_providers}, Got: {actual_provider_names}"
        )

        # Validate each provider configuration
        for provider_config in providers:
            # Check required fields
            assert provider_config.name, f"Provider {provider_config.name} missing name"
            assert provider_config.display_name, (
                f"Provider {provider_config.name} missing display_name"
            )
            assert provider_config.provider_class, (
                f"Provider {provider_config.name} missing provider_class"
            )
            assert provider_config.client_id_env, (
                f"Provider {provider_config.name} missing client_id_env"
            )
            assert provider_config.client_secret_env, (
                f"Provider {provider_config.name} missing client_secret_env"
            )
            assert len(provider_config.scopes) > 0, f"Provider {provider_config.name} has no scopes"

            # Validate provider class can be imported
            try:
                module_path, class_name = provider_config.provider_class.rsplit(".", 1)
                module = __import__(module_path, fromlist=[class_name])
                provider_class = getattr(module, class_name)
                assert provider_class is not None, (
                    f"Provider class {provider_config.provider_class} not found"
                )
            except (ImportError, AttributeError) as e:
                pytest.fail(
                    f"Failed to import provider class {provider_config.provider_class}: {e}"
                )

    def test_oauth_yaml_provider_specific_configurations(self):
        """Test that each provider in oauth.yaml has correct configuration."""

        test_file = Path(__file__)
        # tests/unit/server/test_oauth_provider_name.py -> tests/unit/server -> tests/unit -> tests -> nexus -> configs
        nexus_root = test_file.parent.parent.parent.parent
        oauth_yaml = nexus_root / "configs" / "oauth.yaml"

        if not oauth_yaml.exists():
            raise FileNotFoundError(
                f"oauth.yaml not found at {oauth_yaml}. "
                f"This file is required for OAuth provider configuration."
            )

        factory = OAuthProviderFactory.from_file(oauth_yaml)

        # Test Google Drive provider
        google_drive = factory.get_provider_config("google-drive")
        assert google_drive is not None, "google-drive provider not found"
        assert "https://www.googleapis.com/auth/drive" in google_drive.scopes
        assert google_drive.provider_class == "nexus.server.auth.google_oauth.GoogleOAuthProvider"
        assert google_drive.client_id_env == "NEXUS_OAUTH_GOOGLE_CLIENT_ID"
        assert google_drive.client_secret_env == "NEXUS_OAUTH_GOOGLE_CLIENT_SECRET"
        assert google_drive.requires_pkce is False

        # Test Gmail provider
        gmail = factory.get_provider_config("gmail")
        assert gmail is not None, "gmail provider not found"
        assert "https://www.googleapis.com/auth/gmail.readonly" in gmail.scopes
        assert gmail.provider_class == "nexus.server.auth.google_oauth.GoogleOAuthProvider"
        assert gmail.client_id_env == "NEXUS_OAUTH_GOOGLE_CLIENT_ID"
        assert gmail.client_secret_env == "NEXUS_OAUTH_GOOGLE_CLIENT_SECRET"

        # Test Google Cloud Storage provider
        gcs = factory.get_provider_config("google-cloud-storage")
        assert gcs is not None, "google-cloud-storage provider not found"
        assert "https://www.googleapis.com/auth/devstorage.read_write" in gcs.scopes
        assert gcs.provider_class == "nexus.server.auth.google_oauth.GoogleOAuthProvider"
        assert gcs.client_id_env == "NEXUS_OAUTH_GOOGLE_CLIENT_ID"
        assert gcs.client_secret_env == "NEXUS_OAUTH_GOOGLE_CLIENT_SECRET"

        # Test Microsoft provider
        microsoft = factory.get_provider_config("microsoft-onedrive")
        assert microsoft is not None, "microsoft provider not found"
        assert "Files.ReadWrite.All" in microsoft.scopes
        assert "offline_access" in microsoft.scopes
        assert (
            microsoft.provider_class == "nexus.server.auth.microsoft_oauth.MicrosoftOAuthProvider"
        )
        assert microsoft.client_id_env == "NEXUS_OAUTH_MICROSOFT_CLIENT_ID"
        assert microsoft.client_secret_env == "NEXUS_OAUTH_MICROSOFT_CLIENT_SECRET"
        assert microsoft.requires_pkce is False
        # Verify tenant_id_env is NOT in metadata (we removed it)
        assert "tenant_id_env" not in microsoft.metadata

        # Test X provider
        x = factory.get_provider_config("x")
        assert x is not None, "x provider not found"
        assert "tweet.read" in x.scopes
        assert "offline.access" in x.scopes
        assert x.provider_class == "nexus.server.auth.x_oauth.XOAuthProvider"
        assert x.client_id_env == "NEXUS_OAUTH_X_CLIENT_ID"
        assert x.client_secret_env == "NEXUS_OAUTH_X_CLIENT_SECRET"
        assert x.requires_pkce is True

    def test_oauth_yaml_provider_creation(self):
        """Test that providers can be created from oauth.yaml configuration."""

        test_file = Path(__file__)
        # tests/unit/server/test_oauth_provider_name.py -> tests/unit/server -> tests/unit -> tests -> nexus -> configs
        nexus_root = test_file.parent.parent.parent.parent
        oauth_yaml = nexus_root / "configs" / "oauth.yaml"

        if not oauth_yaml.exists():
            raise FileNotFoundError(
                f"oauth.yaml not found at {oauth_yaml}. "
                f"This file is required for OAuth provider configuration."
            )

        factory = OAuthProviderFactory.from_file(oauth_yaml)

        # Test creating each provider (with mocked credentials)
        providers_to_test = [
            "google-drive",
            "gmail",
            "google-cloud-storage",
            "microsoft-onedrive",
            "x",
        ]

        for provider_name in providers_to_test:
            provider_config = factory.get_provider_config(provider_name)
            assert provider_config is not None, f"Provider {provider_name} not found in config"

            # Mock environment variables
            env_vars = {
                provider_config.client_id_env: "test-client-id",
            }
            if provider_config.client_secret_env:
                env_vars[provider_config.client_secret_env] = "test-client-secret"

            with patch.dict("os.environ", env_vars):
                try:
                    provider = factory.create_provider(
                        name=provider_name,
                        redirect_uri="http://localhost:3000/callback",
                    )

                    # Validate provider was created with correct name
                    assert provider.provider_name == provider_name, (
                        f"Provider {provider_name} has incorrect provider_name: {provider.provider_name}"
                    )
                    assert provider.redirect_uri == "http://localhost:3000/callback"
                    assert len(provider.scopes) > 0, f"Provider {provider_name} has no scopes"

                    # Validate provider type
                    if "google" in provider_name:
                        assert isinstance(provider, GoogleOAuthProvider)
                    elif provider_name == "microsoft-onedrive":
                        assert isinstance(provider, MicrosoftOAuthProvider)
                    elif provider_name == "x":
                        assert isinstance(provider, XOAuthProvider)

                except Exception as e:
                    pytest.fail(f"Failed to create provider {provider_name}: {e}")
