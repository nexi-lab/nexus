"""Unit tests for base authentication provider interface."""

import pytest

from nexus.server.auth.base import AuthProvider, AuthResult


def test_auth_result_basic():
    """Test basic AuthResult creation."""
    result = AuthResult(
        authenticated=True,
        subject_type="user",
        subject_id="alice",
        tenant_id="org_acme",
        is_admin=False,
    )

    assert result.authenticated is True
    assert result.subject_type == "user"
    assert result.subject_id == "alice"
    assert result.tenant_id == "org_acme"
    assert result.is_admin is False
    assert result.metadata is None


def test_auth_result_with_metadata():
    """Test AuthResult with metadata."""
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


def test_auth_result_failed():
    """Test failed authentication result."""
    result = AuthResult(authenticated=False)

    assert result.authenticated is False
    assert result.subject_type == "user"  # default
    assert result.subject_id is None
    assert result.tenant_id is None
    assert result.is_admin is False


def test_auth_result_different_subject_types():
    """Test AuthResult with different subject types."""
    # User
    user_result = AuthResult(
        authenticated=True, subject_type="user", subject_id="alice", tenant_id="org_acme"
    )
    assert user_result.subject_type == "user"

    # Agent
    agent_result = AuthResult(
        authenticated=True,
        subject_type="agent",
        subject_id="agent_claude_001",
        tenant_id="org_acme",
    )
    assert agent_result.subject_type == "agent"

    # Service
    service_result = AuthResult(
        authenticated=True,
        subject_type="service",
        subject_id="backup_bot",
        is_admin=True,
    )
    assert service_result.subject_type == "service"
    assert service_result.is_admin is True

    # Session
    session_result = AuthResult(
        authenticated=True,
        subject_type="session",
        subject_id="session_xyz",
        tenant_id="org_acme",
    )
    assert session_result.subject_type == "session"


def test_auth_result_admin_flag():
    """Test admin flag in AuthResult."""
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
    """Concrete implementation for testing abstract base class."""

    async def authenticate(self, token: str) -> AuthResult:
        """Test implementation."""
        if token == "valid_token":
            return AuthResult(
                authenticated=True,
                subject_type="user",
                subject_id="test_user",
            )
        return AuthResult(authenticated=False)

    async def validate_token(self, token: str) -> bool:
        """Test implementation."""
        return token == "valid_token"

    def close(self) -> None:
        """Test implementation."""
        pass


@pytest.mark.asyncio
async def test_auth_provider_interface():
    """Test that concrete provider implements interface correctly."""
    provider = ConcreteAuthProvider()

    # Test successful authentication
    result = await provider.authenticate("valid_token")
    assert result.authenticated is True
    assert result.subject_id == "test_user"

    # Test failed authentication
    result = await provider.authenticate("invalid_token")
    assert result.authenticated is False

    # Test token validation
    assert await provider.validate_token("valid_token") is True
    assert await provider.validate_token("invalid_token") is False


def test_auth_provider_close():
    """Test that close method exists and can be called."""
    provider = ConcreteAuthProvider()
    # Should not raise
    provider.close()
