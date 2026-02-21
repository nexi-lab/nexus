"""Unit tests for static API key authentication provider."""

import pytest

from nexus.auth.providers.static_key import StaticAPIKeyAuth


@pytest.fixture
def sample_api_keys():
    return {
        "sk-alice-secret-key": {
            "subject_type": "user",
            "subject_id": "alice",
            "zone_id": "org_acme",
            "is_admin": True,
        },
        "sk-agent-secret-key": {
            "subject_type": "agent",
            "subject_id": "agent_claude_001",
            "zone_id": "org_acme",
            "is_admin": False,
        },
        "sk-service-backup-key": {
            "subject_type": "service",
            "subject_id": "backup_service",
            "zone_id": None,
            "is_admin": True,
            "metadata": {"purpose": "backup"},
        },
        "sk-minimal-key": {
            "subject_id": "bob",
            "zone_id": "org_xyz",
            "is_admin": False,
        },
    }


@pytest.fixture
def auth_provider(sample_api_keys):
    return StaticAPIKeyAuth(sample_api_keys)


@pytest.mark.asyncio
async def test_authenticate_user_key(auth_provider):
    """Authenticate a valid user API key."""
    result = await auth_provider.authenticate("sk-alice-secret-key")
    assert result.authenticated is True
    assert result.subject_type == "user"
    assert result.subject_id == "alice"
    assert result.zone_id == "org_acme"
    assert result.is_admin is True


@pytest.mark.asyncio
async def test_authenticate_agent_key(auth_provider):
    """Authenticate a valid agent API key."""
    result = await auth_provider.authenticate("sk-agent-secret-key")
    assert result.authenticated is True
    assert result.subject_type == "agent"
    assert result.subject_id == "agent_claude_001"
    assert result.zone_id == "org_acme"
    assert result.is_admin is False


@pytest.mark.asyncio
async def test_authenticate_service_key_with_metadata(auth_provider):
    """Authenticate a service key with metadata."""
    result = await auth_provider.authenticate("sk-service-backup-key")
    assert result.authenticated is True
    assert result.subject_type == "service"
    assert result.subject_id == "backup_service"
    assert result.zone_id is None
    assert result.is_admin is True
    assert result.metadata == {"purpose": "backup"}


@pytest.mark.asyncio
async def test_authenticate_minimal_key(auth_provider):
    """Authenticate a key with minimal config (no subject_type specified)."""
    result = await auth_provider.authenticate("sk-minimal-key")
    assert result.authenticated is True
    assert result.subject_type == "user"  # default
    assert result.subject_id == "bob"
    assert result.zone_id == "org_xyz"
    assert result.is_admin is False


@pytest.mark.asyncio
async def test_authenticate_invalid_key(auth_provider):
    """Authenticate with an unknown key returns failure."""
    result = await auth_provider.authenticate("sk-unknown-key")
    assert result.authenticated is False


@pytest.mark.asyncio
async def test_authenticate_empty_token(auth_provider):
    """Authenticate with empty string returns failure."""
    result = await auth_provider.authenticate("")
    assert result.authenticated is False


@pytest.mark.asyncio
async def test_authenticate_bad_prefix(auth_provider):
    """Authenticate with a key not starting with sk- returns failure."""
    result = await auth_provider.authenticate("bad-prefix-key")
    assert result.authenticated is False


@pytest.mark.asyncio
async def test_validate_token_valid(auth_provider):
    """validate_token returns True for known keys."""
    assert await auth_provider.validate_token("sk-alice-secret-key") is True


@pytest.mark.asyncio
async def test_validate_token_invalid(auth_provider):
    """validate_token returns False for unknown keys."""
    assert await auth_provider.validate_token("sk-unknown-key") is False


def test_close(auth_provider):
    """close() doesn't raise."""
    auth_provider.close()


def test_from_config():
    """Create StaticAPIKeyAuth from config dictionary."""
    config = {
        "api_keys": {
            "sk-config-key": {
                "subject_type": "user",
                "subject_id": "config_user",
                "zone_id": "org_config",
                "is_admin": False,
            }
        }
    }
    provider = StaticAPIKeyAuth.from_config(config)
    assert len(provider.api_keys) == 1
    assert "sk-config-key" in provider.api_keys


def test_from_config_empty():
    """Create StaticAPIKeyAuth from empty config."""
    provider = StaticAPIKeyAuth.from_config({})
    assert len(provider.api_keys) == 0


@pytest.mark.asyncio
async def test_zone_id_none_when_not_provided():
    """When zone_id is not in the key config, result.zone_id is None."""
    provider = StaticAPIKeyAuth(
        {
            "sk-no-zone-key": {
                "subject_type": "user",
                "subject_id": "alice",
                "is_admin": False,
            }
        }
    )
    result = await provider.authenticate("sk-no-zone-key")
    assert result.authenticated is True
    assert result.zone_id is None
