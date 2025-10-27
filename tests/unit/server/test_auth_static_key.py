"""Unit tests for static API key authentication provider."""

import pytest

from nexus.server.auth.static_key import StaticAPIKeyAuth


@pytest.fixture
def sample_api_keys():
    """Sample API keys configuration."""
    return {
        "sk-alice-secret-key": {
            "subject_type": "user",
            "subject_id": "alice",
            "tenant_id": "org_acme",
            "is_admin": True,
        },
        "sk-agent-secret-key": {
            "subject_type": "agent",
            "subject_id": "agent_claude_001",
            "tenant_id": "org_acme",
            "is_admin": False,
        },
        "sk-service-backup-key": {
            "subject_type": "service",
            "subject_id": "backup_service",
            "tenant_id": None,
            "is_admin": True,
            "metadata": {"purpose": "backup"},
        },
        "sk-legacy-user-key": {
            "user_id": "bob",  # Old format without subject_type
            "tenant_id": "org_xyz",
            "is_admin": False,
        },
    }


@pytest.fixture
def auth_provider(sample_api_keys):
    """Create StaticAPIKeyAuth instance."""
    return StaticAPIKeyAuth(sample_api_keys)


@pytest.mark.asyncio
async def test_authenticate_valid_user(auth_provider):
    """Test authentication with valid user API key."""
    result = await auth_provider.authenticate("sk-alice-secret-key")

    assert result.authenticated is True
    assert result.subject_type == "user"
    assert result.subject_id == "alice"
    assert result.tenant_id == "org_acme"
    assert result.is_admin is True


@pytest.mark.asyncio
async def test_authenticate_valid_agent(auth_provider):
    """Test authentication with valid agent API key."""
    result = await auth_provider.authenticate("sk-agent-secret-key")

    assert result.authenticated is True
    assert result.subject_type == "agent"
    assert result.subject_id == "agent_claude_001"
    assert result.tenant_id == "org_acme"
    assert result.is_admin is False


@pytest.mark.asyncio
async def test_authenticate_valid_service(auth_provider):
    """Test authentication with valid service API key."""
    result = await auth_provider.authenticate("sk-service-backup-key")

    assert result.authenticated is True
    assert result.subject_type == "service"
    assert result.subject_id == "backup_service"
    assert result.tenant_id is None
    assert result.is_admin is True
    assert result.metadata == {"purpose": "backup"}


@pytest.mark.asyncio
async def test_authenticate_legacy_format(auth_provider):
    """Test authentication with legacy user_id format."""
    result = await auth_provider.authenticate("sk-legacy-user-key")

    assert result.authenticated is True
    assert result.subject_type == "user"  # defaults to user
    assert result.subject_id == "bob"  # falls back to user_id
    assert result.tenant_id == "org_xyz"
    assert result.is_admin is False


@pytest.mark.asyncio
async def test_authenticate_invalid_key(auth_provider):
    """Test authentication with invalid API key."""
    result = await auth_provider.authenticate("sk-invalid-key")

    assert result.authenticated is False


@pytest.mark.asyncio
async def test_authenticate_empty_token(auth_provider):
    """Test authentication with empty token."""
    result = await auth_provider.authenticate("")

    assert result.authenticated is False


@pytest.mark.asyncio
async def test_authenticate_missing_prefix(auth_provider):
    """Test authentication with key missing sk- prefix."""
    result = await auth_provider.authenticate("alice-secret-key")

    assert result.authenticated is False


@pytest.mark.asyncio
async def test_validate_token_valid(auth_provider):
    """Test token validation with valid key."""
    assert await auth_provider.validate_token("sk-alice-secret-key") is True


@pytest.mark.asyncio
async def test_validate_token_invalid(auth_provider):
    """Test token validation with invalid key."""
    assert await auth_provider.validate_token("sk-invalid-key") is False


def test_from_config_with_keys():
    """Test creating provider from configuration dictionary."""
    config = {
        "api_keys": {
            "sk-test-key": {
                "subject_type": "user",
                "subject_id": "test_user",
                "is_admin": True,
            }
        }
    }

    auth = StaticAPIKeyAuth.from_config(config)
    assert len(auth.api_keys) == 1
    assert "sk-test-key" in auth.api_keys


def test_from_config_empty():
    """Test creating provider from empty configuration."""
    config = {}

    auth = StaticAPIKeyAuth.from_config(config)
    assert len(auth.api_keys) == 0


def test_from_config_no_api_keys_field():
    """Test creating provider from config without api_keys field."""
    config = {"other_field": "value"}

    auth = StaticAPIKeyAuth.from_config(config)
    assert len(auth.api_keys) == 0


def test_initialization():
    """Test basic initialization."""
    api_keys = {"sk-test": {"subject_id": "user1", "is_admin": False}}
    auth = StaticAPIKeyAuth(api_keys)

    assert auth.api_keys == api_keys


def test_close():
    """Test close method (should do nothing for static auth)."""
    auth = StaticAPIKeyAuth({})
    # Should not raise
    auth.close()
