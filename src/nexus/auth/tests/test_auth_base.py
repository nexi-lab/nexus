"""Unit tests for base authentication provider interface."""

from __future__ import annotations

import pytest

from nexus.auth.providers.base import AuthProvider, AuthResult


def test_auth_result_basic():
    """Create AuthResult with all fields, assert values."""
    result = AuthResult(
        authenticated=True,
        subject_type="user",
        subject_id="alice",
        zone_id="org_acme",
        is_admin=False,
    )
    assert result.authenticated is True
    assert result.subject_type == "user"
    assert result.subject_id == "alice"
    assert result.zone_id == "org_acme"
    assert result.is_admin is False
    assert result.metadata is None
    assert result.agent_generation is None
    assert result.inherit_permissions is True


def test_auth_result_with_metadata():
    """AuthResult with metadata dict."""
    metadata = {"key_id": "key_123", "key_name": "Test Key"}
    result = AuthResult(
        authenticated=True,
        subject_type="agent",
        subject_id="agent_123",
        metadata=metadata,
    )
    assert result.authenticated is True
    assert result.subject_type == "agent"
    assert result.subject_id == "agent_123"
    assert result.metadata == metadata
    assert result.metadata["key_id"] == "key_123"
    assert result.metadata["key_name"] == "Test Key"


def test_auth_result_failed():
    """AuthResult(authenticated=False), check defaults."""
    result = AuthResult(authenticated=False)
    assert result.authenticated is False
    assert result.subject_type == "user"
    assert result.subject_id is None
    assert result.zone_id is None
    assert result.is_admin is False
    assert result.metadata is None
    assert result.agent_generation is None
    assert result.inherit_permissions is True


def test_auth_result_different_subject_types():
    """Test user, agent, service, session subject types."""
    user_result = AuthResult(
        authenticated=True,
        subject_type="user",
        subject_id="alice",
        zone_id="org_acme",
    )
    assert user_result.subject_type == "user"

    agent_result = AuthResult(
        authenticated=True,
        subject_type="agent",
        subject_id="agent_claude_001",
        zone_id="org_acme",
    )
    assert agent_result.subject_type == "agent"

    service_result = AuthResult(
        authenticated=True,
        subject_type="service",
        subject_id="backup_bot",
        is_admin=True,
    )
    assert service_result.subject_type == "service"
    assert service_result.is_admin is True

    session_result = AuthResult(
        authenticated=True,
        subject_type="session",
        subject_id="session_xyz",
        zone_id="org_acme",
    )
    assert session_result.subject_type == "session"


def test_auth_result_admin_flag():
    """Test is_admin True/False."""
    admin_result = AuthResult(
        authenticated=True,
        subject_type="user",
        subject_id="admin_user",
        is_admin=True,
    )
    assert admin_result.is_admin is True

    normal_result = AuthResult(
        authenticated=True,
        subject_type="user",
        subject_id="normal_user",
        is_admin=False,
    )
    assert normal_result.is_admin is False


class ConcreteAuthProvider(AuthProvider):
    """Concrete provider for testing the ABC interface."""

    async def authenticate(self, token: str) -> AuthResult:
        if token == "valid_token":
            return AuthResult(
                authenticated=True,
                subject_type="user",
                subject_id="test_user",
            )
        return AuthResult(authenticated=False)

    async def validate_token(self, token: str) -> bool:
        return token == "valid_token"

    def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_auth_provider_interface():
    """Async test with concrete provider implementing authenticate/validate_token/close."""
    provider = ConcreteAuthProvider()

    result = await provider.authenticate("valid_token")
    assert result.authenticated is True
    assert result.subject_id == "test_user"

    result = await provider.authenticate("invalid_token")
    assert result.authenticated is False

    assert await provider.validate_token("valid_token") is True
    assert await provider.validate_token("invalid_token") is False


def test_auth_provider_close():
    """close() doesn't raise."""
    provider = ConcreteAuthProvider()
    provider.close()
