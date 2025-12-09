"""Unit tests for OAuth to Klavis MCP mappings."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from nexus.mcp.oauth_mappings import OAuthKlavisMappings, OAuthProviderMapping


class TestOAuthProviderMapping:
    """Test OAuthProviderMapping dataclass."""

    def test_provider_mapping_creation(self) -> None:
        """Test creating OAuthProviderMapping instance."""
        mapping = OAuthProviderMapping(
            name="test",
            local_providers=["test-provider"],
            connectors=["test_connector"],
            klavis_mcps=["test_mcp"],
            mcp_scopes={"test_mcp": ["scope1", "scope2"]},
        )

        assert mapping.name == "test"
        assert mapping.local_providers == ["test-provider"]
        assert mapping.connectors == ["test_connector"]
        assert mapping.klavis_mcps == ["test_mcp"]
        assert mapping.mcp_scopes == {"test_mcp": ["scope1", "scope2"]}

    def test_provider_mapping_defaults(self) -> None:
        """Test OAuthProviderMapping with default values."""
        mapping = OAuthProviderMapping(name="test")

        assert mapping.name == "test"
        assert mapping.local_providers == []
        assert mapping.connectors == []
        assert mapping.klavis_mcps == []
        assert mapping.mcp_scopes == {}

    def test_provider_mapping_from_dict(self) -> None:
        """Test creating from dictionary."""
        data = {
            "local_providers": ["provider1", "provider2"],
            "connectors": ["connector1"],
            "klavis_mcps": ["mcp1", "mcp2"],
            "mcp_scopes": {
                "mcp1": ["scope1"],
                "mcp2": ["scope2", "scope3"],
            },
        }

        mapping = OAuthProviderMapping.from_dict("test", data)

        assert mapping.name == "test"
        assert mapping.local_providers == ["provider1", "provider2"]
        assert mapping.connectors == ["connector1"]
        assert mapping.klavis_mcps == ["mcp1", "mcp2"]
        assert mapping.mcp_scopes["mcp1"] == ["scope1"]
        assert mapping.mcp_scopes["mcp2"] == ["scope2", "scope3"]

    def test_provider_mapping_from_dict_empty(self) -> None:
        """Test creating from empty dictionary."""
        mapping = OAuthProviderMapping.from_dict("test", {})

        assert mapping.name == "test"
        assert mapping.local_providers == []
        assert mapping.connectors == []
        assert mapping.klavis_mcps == []
        assert mapping.mcp_scopes == {}


class TestOAuthKlavisMappingsInit:
    """Test OAuthKlavisMappings initialization."""

    def test_init_empty(self) -> None:
        """Test initializing with no providers."""
        mappings = OAuthKlavisMappings()
        assert mappings.list_oauth_providers() == []

    def test_init_with_providers(self) -> None:
        """Test initializing with providers."""
        providers = {
            "google": OAuthProviderMapping(
                name="google",
                connectors=["gdrive_connector"],
                klavis_mcps=["gmail", "google_drive"],
            ),
            "microsoft": OAuthProviderMapping(
                name="microsoft",
                klavis_mcps=["outlook"],
            ),
        }

        mappings = OAuthKlavisMappings(providers=providers)
        assert len(mappings.list_oauth_providers()) == 2
        assert "google" in mappings.list_oauth_providers()
        assert "microsoft" in mappings.list_oauth_providers()


class TestOAuthKlavisMappingsFromYaml:
    """Test loading mappings from YAML file."""

    def test_from_yaml_valid_file(self) -> None:
        """Test loading from valid YAML file."""
        yaml_content = """
oauth_providers:
  google:
    local_providers:
      - google-drive
    connectors:
      - gdrive_connector
    klavis_mcps:
      - gmail
      - google_drive
    mcp_scopes:
      gmail:
        - https://www.googleapis.com/auth/gmail.readonly
      google_drive:
        - https://www.googleapis.com/auth/drive
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name

        try:
            mappings = OAuthKlavisMappings.from_yaml(temp_path)

            assert "google" in mappings.list_oauth_providers()
            assert mappings.get_oauth_provider_for_klavis_mcp("gmail") == "google"
            assert mappings.get_oauth_provider_for_connector("gdrive_connector") == "google"
            assert mappings.get_required_scopes("gmail") == [
                "https://www.googleapis.com/auth/gmail.readonly"
            ]
        finally:
            Path(temp_path).unlink()

    def test_from_yaml_nonexistent_file(self) -> None:
        """Test loading from non-existent file."""
        mappings = OAuthKlavisMappings.from_yaml("/nonexistent/path/file.yaml")
        assert mappings.list_oauth_providers() == []

    def test_from_yaml_empty_file(self) -> None:
        """Test loading from empty YAML file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("")
            temp_path = f.name

        try:
            mappings = OAuthKlavisMappings.from_yaml(temp_path)
            assert mappings.list_oauth_providers() == []
        finally:
            Path(temp_path).unlink()


class TestOAuthKlavisMappingsLoadDefault:
    """Test loading default mappings."""

    def test_load_default_with_env_var(self) -> None:
        """Test loading with environment variable."""
        yaml_content = """
oauth_providers:
  test:
    klavis_mcps:
      - test_mcp
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name

        try:
            with patch.dict("os.environ", {"NEXUS_OAUTH_MAPPINGS_PATH": temp_path}):
                mappings = OAuthKlavisMappings.load_default()
                assert "test" in mappings.list_oauth_providers()
        finally:
            Path(temp_path).unlink()

    def test_load_default_fallback_to_builtin(self) -> None:
        """Test fallback to built-in defaults when no file found."""
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("pathlib.Path.exists", return_value=False),
        ):
            # Mock Path.exists to always return False
            mappings = OAuthKlavisMappings.load_default()
            # Should have built-in defaults
            assert "google" in mappings.list_oauth_providers()
            assert "microsoft" in mappings.list_oauth_providers()


class TestOAuthKlavisMappingsWithBuiltinDefaults:
    """Test built-in default mappings."""

    def test_builtin_defaults(self) -> None:
        """Test creating with built-in defaults."""
        mappings = OAuthKlavisMappings.with_builtin_defaults()

        providers = mappings.list_oauth_providers()
        assert "google" in providers
        assert "microsoft" in providers
        assert "twitter" in providers

    def test_builtin_google_mappings(self) -> None:
        """Test Google provider built-in mappings."""
        mappings = OAuthKlavisMappings.with_builtin_defaults()

        assert mappings.get_oauth_provider_for_klavis_mcp("gmail") == "google"
        assert mappings.get_oauth_provider_for_klavis_mcp("google_drive") == "google"
        assert mappings.get_oauth_provider_for_connector("gdrive_connector") == "google"

        # Check scopes
        gmail_scopes = mappings.get_required_scopes("gmail")
        assert "https://www.googleapis.com/auth/gmail.readonly" in gmail_scopes

    def test_builtin_microsoft_mappings(self) -> None:
        """Test Microsoft provider built-in mappings."""
        mappings = OAuthKlavisMappings.with_builtin_defaults()

        assert mappings.get_oauth_provider_for_klavis_mcp("outlook") == "microsoft"
        assert mappings.get_oauth_provider_for_klavis_mcp("onedrive") == "microsoft"


class TestOAuthKlavisMappingsGetOAuthProvider:
    """Test getting OAuth provider for various identifiers."""

    def test_get_oauth_provider_for_klavis_mcp(self) -> None:
        """Test getting OAuth provider from Klavis MCP name."""
        mappings = OAuthKlavisMappings.with_builtin_defaults()

        assert mappings.get_oauth_provider_for_klavis_mcp("gmail") == "google"
        assert mappings.get_oauth_provider_for_klavis_mcp("google_drive") == "google"
        assert mappings.get_oauth_provider_for_klavis_mcp("outlook") == "microsoft"
        assert mappings.get_oauth_provider_for_klavis_mcp("nonexistent") is None

    def test_get_oauth_provider_for_connector(self) -> None:
        """Test getting OAuth provider from connector name."""
        mappings = OAuthKlavisMappings.with_builtin_defaults()

        assert mappings.get_oauth_provider_for_connector("gdrive_connector") == "google"
        assert mappings.get_oauth_provider_for_connector("x_connector") == "twitter"
        assert mappings.get_oauth_provider_for_connector("nonexistent") is None

    def test_get_oauth_provider_for_local_provider(self) -> None:
        """Test getting OAuth provider from local provider name."""
        mappings = OAuthKlavisMappings.with_builtin_defaults()

        assert mappings.get_oauth_provider_for_local_provider("google-drive") == "google"
        assert mappings.get_oauth_provider_for_local_provider("gmail") == "google"
        assert mappings.get_oauth_provider_for_local_provider("x") == "twitter"
        assert mappings.get_oauth_provider_for_local_provider("nonexistent") is None


class TestOAuthKlavisMappingsGetRequiredScopes:
    """Test getting required scopes for MCP."""

    def test_get_required_scopes_exists(self) -> None:
        """Test getting scopes for MCP with defined scopes."""
        mappings = OAuthKlavisMappings.with_builtin_defaults()

        scopes = mappings.get_required_scopes("gmail")
        assert isinstance(scopes, list)
        assert len(scopes) > 0
        assert "https://www.googleapis.com/auth/gmail.readonly" in scopes

    def test_get_required_scopes_not_found(self) -> None:
        """Test getting scopes for non-existent MCP."""
        mappings = OAuthKlavisMappings.with_builtin_defaults()

        scopes = mappings.get_required_scopes("nonexistent")
        assert scopes == []


class TestOAuthKlavisMappingsGetReusableKlavisMcps:
    """Test getting reusable Klavis MCPs for OAuth provider."""

    def test_get_reusable_klavis_mcps(self) -> None:
        """Test getting MCPs that can reuse Google tokens."""
        mappings = OAuthKlavisMappings.with_builtin_defaults()

        mcps = mappings.get_reusable_klavis_mcps("google")
        assert isinstance(mcps, list)
        assert "gmail" in mcps
        assert "google_drive" in mcps
        assert "google_docs" in mcps

    def test_get_reusable_klavis_mcps_empty(self) -> None:
        """Test getting MCPs for Twitter (has no MCPs)."""
        mappings = OAuthKlavisMappings.with_builtin_defaults()

        mcps = mappings.get_reusable_klavis_mcps("twitter")
        assert mcps == []

    def test_get_reusable_klavis_mcps_not_found(self) -> None:
        """Test getting MCPs for non-existent provider."""
        mappings = OAuthKlavisMappings.with_builtin_defaults()

        mcps = mappings.get_reusable_klavis_mcps("nonexistent")
        assert mcps == []


class TestOAuthKlavisMappingsCanReuseToken:
    """Test checking if token can be reused."""

    def test_can_reuse_token_sufficient_scopes(self) -> None:
        """Test token with sufficient scopes."""
        mappings = OAuthKlavisMappings.with_builtin_defaults()

        token_scopes = [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/userinfo.email",
        ]

        assert mappings.can_reuse_token("gmail", token_scopes) is True

    def test_can_reuse_token_insufficient_scopes(self) -> None:
        """Test token with insufficient scopes."""
        mappings = OAuthKlavisMappings.with_builtin_defaults()

        token_scopes = ["https://www.googleapis.com/auth/userinfo.email"]

        assert mappings.can_reuse_token("gmail", token_scopes) is False

    def test_can_reuse_token_no_required_scopes(self) -> None:
        """Test MCP with no specific scope requirements."""
        mappings = OAuthKlavisMappings()

        # Empty mappings, so no required scopes
        assert mappings.can_reuse_token("any_mcp", []) is True

    def test_can_reuse_token_exact_match(self) -> None:
        """Test token with exact required scopes."""
        mappings = OAuthKlavisMappings.with_builtin_defaults()

        token_scopes = ["https://www.googleapis.com/auth/gmail.readonly"]

        assert mappings.can_reuse_token("gmail", token_scopes) is True


class TestOAuthKlavisMappingsGetMapping:
    """Test getting full mapping for provider."""

    def test_get_mapping_exists(self) -> None:
        """Test getting mapping for existing provider."""
        mappings = OAuthKlavisMappings.with_builtin_defaults()

        mapping = mappings.get_mapping("google")
        assert mapping is not None
        assert mapping.name == "google"
        assert "gmail" in mapping.klavis_mcps

    def test_get_mapping_not_found(self) -> None:
        """Test getting mapping for non-existent provider."""
        mappings = OAuthKlavisMappings.with_builtin_defaults()

        mapping = mappings.get_mapping("nonexistent")
        assert mapping is None
