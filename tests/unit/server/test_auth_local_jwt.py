"""Unit tests for LocalAuth JWT token creation and authentication.

Tests cover Issue #1445: agent_generation in JWT claims.
- create_token() includes agent_generation when present
- create_token() omits agent_generation when absent
- authenticate() extracts agent_generation into AuthResult
- Round-trip: create_token → authenticate preserves agent_generation
"""

from __future__ import annotations

import pytest

from nexus.server.auth.local import LocalAuth


@pytest.fixture()
def auth() -> LocalAuth:
    """Create a LocalAuth instance with a fixed secret for testing."""
    return LocalAuth(jwt_secret="test-secret-for-jwt-unit-tests", token_expiry=3600)


class TestCreateTokenAgentGeneration:
    """Tests for agent_generation in JWT claims via create_token()."""

    def test_token_includes_agent_generation_when_present(self, auth: LocalAuth):
        """create_token should embed agent_generation in JWT when provided."""
        user_info = {
            "subject_type": "agent",
            "subject_id": "agent-001",
            "zone_id": "org_acme",
            "is_admin": False,
            "agent_generation": 5,
        }
        token = auth.create_token("agent@example.com", user_info)
        claims = auth.verify_token(token)

        assert claims["agent_generation"] == 5
        assert claims["subject_type"] == "agent"
        assert claims["subject_id"] == "agent-001"

    def test_token_omits_agent_generation_when_none(self, auth: LocalAuth):
        """create_token should NOT include agent_generation key when None."""
        user_info = {
            "subject_type": "user",
            "subject_id": "alice",
            "zone_id": "org_acme",
            "is_admin": False,
        }
        token = auth.create_token("alice@example.com", user_info)
        claims = auth.verify_token(token)

        assert "agent_generation" not in claims

    def test_token_omits_agent_generation_when_absent(self, auth: LocalAuth):
        """create_token should NOT include agent_generation when not in user_info."""
        user_info = {
            "subject_type": "agent",
            "subject_id": "agent-002",
            "zone_id": "org_acme",
            "is_admin": False,
            # No agent_generation key at all
        }
        token = auth.create_token("agent2@example.com", user_info)
        claims = auth.verify_token(token)

        assert "agent_generation" not in claims

    def test_token_agent_generation_zero(self, auth: LocalAuth):
        """agent_generation=0 is a valid value and should be included."""
        user_info = {
            "subject_type": "agent",
            "subject_id": "agent-003",
            "agent_generation": 0,
        }
        token = auth.create_token("agent3@example.com", user_info)
        claims = auth.verify_token(token)

        assert claims["agent_generation"] == 0


class TestAuthenticateAgentGeneration:
    """Tests for agent_generation extraction in authenticate()."""

    @pytest.mark.asyncio
    async def test_authenticate_returns_agent_generation(self, auth: LocalAuth):
        """authenticate() should extract agent_generation from JWT claims."""
        user_info = {
            "subject_type": "agent",
            "subject_id": "agent-001",
            "agent_generation": 7,
        }
        token = auth.create_token("agent@example.com", user_info)
        result = await auth.authenticate(token)

        assert result.authenticated is True
        assert result.agent_generation == 7
        assert result.subject_type == "agent"
        assert result.subject_id == "agent-001"

    @pytest.mark.asyncio
    async def test_authenticate_returns_none_generation_for_user(self, auth: LocalAuth):
        """authenticate() should return None agent_generation for user tokens."""
        user_info = {
            "subject_type": "user",
            "subject_id": "alice",
        }
        token = auth.create_token("alice@example.com", user_info)
        result = await auth.authenticate(token)

        assert result.authenticated is True
        assert result.agent_generation is None

    @pytest.mark.asyncio
    async def test_authenticate_roundtrip_preserves_generation(self, auth: LocalAuth):
        """Full roundtrip: create_token → authenticate should preserve generation."""
        for gen in [0, 1, 42, 999]:
            user_info = {
                "subject_type": "agent",
                "subject_id": f"agent-gen-{gen}",
                "agent_generation": gen,
            }
            token = auth.create_token(f"agent{gen}@example.com", user_info)
            result = await auth.authenticate(token)

            assert result.agent_generation == gen, f"Failed for generation={gen}"

    @pytest.mark.asyncio
    async def test_authenticate_invalid_token_returns_unauthenticated(self, auth: LocalAuth):
        """Invalid token should return unauthenticated with no generation."""
        result = await auth.authenticate("not-a-valid-jwt")

        assert result.authenticated is False
        assert result.agent_generation is None
