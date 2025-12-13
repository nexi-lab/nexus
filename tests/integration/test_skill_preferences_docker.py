#!/usr/bin/env python3
"""Docker integration tests for skill preferences RPC API.

This test suite tests the full skill preferences workflow including:
- Agent registration
- Setting skill preferences
- Listing skills with preference filtering
- All RPC methods via HTTP

Requires:
- Docker Nexus server running on localhost:8080
- Valid API key in environment or default admin key
"""

import os
from typing import Any

import pytest
import requests

# Test configuration
BASE_URL = os.getenv("NEXUS_SERVER_URL", "http://localhost:8080/api/nfs")
API_KEY = os.getenv("NEXUS_API_KEY", "sk-default_admin_d38a7427_244c5f756dcc064eea6e68a64aa2111e")


def call_rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Call an RPC method via HTTP."""
    url = f"{BASE_URL}/{method}"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
    }

    rpc_request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }

    response = requests.post(url, json=rpc_request, headers=headers, timeout=10)
    response.raise_for_status()

    response_json = response.json()

    # Check for errors
    if "error" in response_json:
        error = response_json["error"]
        raise RuntimeError(
            f"RPC error: {error.get('message', 'Unknown error')} (code: {error.get('code')})"
        )

    return response_json.get("result", {})


@pytest.fixture(scope="module")
def test_user_id():
    """Test user ID."""
    return "docker_test_user"


@pytest.fixture(scope="module")
def test_agent_id():
    """Test agent ID."""
    return "docker_test_agent"


@pytest.fixture(scope="module")
def registered_agent(test_user_id: str, test_agent_id: str) -> dict[str, Any]:
    """Register a test agent and return agent info."""
    try:
        # Try to register agent (may fail if already exists, that's OK)
        agent = call_rpc(
            "register_agent",
            {
                "agent_id": test_agent_id,
                "name": "Docker Test Agent",
                "description": "Agent for skill preferences testing",
            },
        )
        return agent
    except RuntimeError as e:
        # Agent might already exist, that's fine
        if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
            # Try to get existing agent
            try:
                agent = call_rpc("get_agent", {"agent_id": test_agent_id})
                return agent
            except RuntimeError:
                # If we can't get it, just continue - tests will use the agent_id
                return {"agent_id": test_agent_id, "user_id": test_user_id}
        raise


class TestSkillPreferencesDockerIntegration:
    """Docker integration tests for skill preferences."""

    def test_server_health(self):
        """Test that server is accessible."""
        response = requests.get(BASE_URL.replace("/api/nfs", "/health"), timeout=5)
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "healthy"

    def test_register_agent(self, test_user_id: str, test_agent_id: str):
        """Test agent registration."""
        try:
            agent = call_rpc(
                "register_agent",
                {
                    "agent_id": f"{test_agent_id}_new",
                    "name": "New Test Agent",
                    "description": "Test agent registration",
                },
            )
            assert agent["agent_id"] == f"{test_agent_id}_new"
            assert "user_id" in agent
        except RuntimeError as e:
            # Agent might already exist, skip if so
            if "already exists" not in str(e).lower():
                raise

    def test_set_skill_preference(self, test_user_id: str, test_agent_id: str):
        """Test setting a skill preference."""
        # Get available skills first
        all_skills = call_rpc("skills_list", {})
        assert len(all_skills.get("skills", [])) > 0

        test_skill = all_skills["skills"][0]["name"]

        # Set preference to disable
        result = call_rpc(
            "set_skill_preference",
            {
                "user_id": test_user_id,
                "agent_id": test_agent_id,
                "skill_name": test_skill,
                "enabled": False,
                "reason": "Docker integration test",
            },
        )

        assert result["user_id"] == test_user_id
        assert result["agent_id"] == test_agent_id
        assert result["skill_name"] == test_skill
        assert result["enabled"] is False

        # Cleanup
        call_rpc(
            "delete_skill_preference",
            {
                "user_id": test_user_id,
                "agent_id": test_agent_id,
                "skill_name": test_skill,
            },
        )

    def test_is_skill_enabled(self, test_user_id: str, test_agent_id: str):
        """Test checking if skill is enabled."""
        # Get available skills
        all_skills = call_rpc("skills_list", {})
        test_skill = all_skills["skills"][0]["name"]

        # Disable skill
        call_rpc(
            "set_skill_preference",
            {
                "user_id": test_user_id,
                "agent_id": test_agent_id,
                "skill_name": test_skill,
                "enabled": False,
            },
        )

        # Check if enabled (should be False)
        result = call_rpc(
            "is_skill_enabled",
            {
                "user_id": test_user_id,
                "agent_id": test_agent_id,
                "skill_name": test_skill,
            },
        )
        assert result["enabled"] is False

        # Cleanup
        call_rpc(
            "delete_skill_preference",
            {
                "user_id": test_user_id,
                "agent_id": test_agent_id,
                "skill_name": test_skill,
            },
        )

        # Check again (should default to True)
        result = call_rpc(
            "is_skill_enabled",
            {
                "user_id": test_user_id,
                "agent_id": test_agent_id,
                "skill_name": test_skill,
            },
        )
        assert result["enabled"] is True

    def test_list_skill_preferences(self, test_user_id: str, test_agent_id: str):
        """Test listing skill preferences."""
        # Get available skills
        all_skills = call_rpc("skills_list", {})
        assert len(all_skills.get("skills", [])) >= 2

        skill1 = all_skills["skills"][0]["name"]
        skill2 = all_skills["skills"][1]["name"]

        # Create preferences
        call_rpc(
            "set_skill_preference",
            {
                "user_id": test_user_id,
                "agent_id": test_agent_id,
                "skill_name": skill1,
                "enabled": False,
            },
        )
        call_rpc(
            "set_skill_preference",
            {
                "user_id": test_user_id,
                "agent_id": test_agent_id,
                "skill_name": skill2,
                "enabled": True,
            },
        )

        # List all preferences
        result = call_rpc(
            "list_skill_preferences", {"user_id": test_user_id, "agent_id": test_agent_id}
        )
        prefs = result["preferences"]
        assert len(prefs) >= 2

        # List only enabled
        result = call_rpc(
            "list_skill_preferences",
            {
                "user_id": test_user_id,
                "agent_id": test_agent_id,
                "enabled_only": True,
            },
        )
        enabled_prefs = result["preferences"]
        assert all(p["enabled"] for p in enabled_prefs)
        assert any(p["skill_name"] == skill2 for p in enabled_prefs)

        # List only disabled
        result = call_rpc(
            "list_skill_preferences",
            {
                "user_id": test_user_id,
                "agent_id": test_agent_id,
                "enabled_only": False,
            },
        )
        disabled_prefs = result["preferences"]
        assert all(not p["enabled"] for p in disabled_prefs)
        assert any(p["skill_name"] == skill1 for p in disabled_prefs)

        # Cleanup
        call_rpc(
            "delete_skill_preference",
            {"user_id": test_user_id, "agent_id": test_agent_id, "skill_name": skill1},
        )
        call_rpc(
            "delete_skill_preference",
            {"user_id": test_user_id, "agent_id": test_agent_id, "skill_name": skill2},
        )

    def test_skills_list_with_preference_filtering(self, test_user_id: str, test_agent_id: str):
        """Test skills_list filtered by preferences."""
        # Get all skills
        all_skills_result = call_rpc("skills_list", {})
        all_skills = all_skills_result["skills"]
        assert len(all_skills) > 0

        test_skill = all_skills[0]["name"]

        # Disable the skill
        call_rpc(
            "set_skill_preference",
            {
                "user_id": test_user_id,
                "agent_id": test_agent_id,
                "skill_name": test_skill,
                "enabled": False,
            },
        )

        # List only enabled skills
        enabled_result = call_rpc(
            "skills_list",
            {
                "user_id": test_user_id,
                "agent_id": test_agent_id,
                "enabled_only": True,
            },
        )
        enabled_skills = enabled_result["skills"]
        enabled_names = [s["name"] for s in enabled_skills]

        # Verify test_skill is filtered out
        assert test_skill not in enabled_names
        assert len(enabled_skills) < len(all_skills)

        # List all skills with enabled flags
        all_with_flags_result = call_rpc(
            "skills_list",
            {
                "user_id": test_user_id,
                "agent_id": test_agent_id,
                "enabled_only": None,
            },
        )
        all_with_flags = all_with_flags_result["skills"]

        # Find test_skill and verify it's marked as disabled
        test_skill_data = next(s for s in all_with_flags if s["name"] == test_skill)
        assert test_skill_data["enabled"] is False

        # Verify other skills are marked as enabled
        other_skills = [s for s in all_with_flags if s["name"] != test_skill]
        assert all(s.get("enabled", True) for s in other_skills)

        # Cleanup
        call_rpc(
            "delete_skill_preference",
            {
                "user_id": test_user_id,
                "agent_id": test_agent_id,
                "skill_name": test_skill,
            },
        )

    def test_skills_list_disabled_only(self, test_user_id: str, test_agent_id: str):
        """Test skills_list with enabled_only=False."""
        # Get all skills
        all_skills_result = call_rpc("skills_list", {})
        all_skills = all_skills_result["skills"]
        assert len(all_skills) >= 2

        skill1 = all_skills[0]["name"]
        skill2 = all_skills[1]["name"]

        # Disable both skills
        call_rpc(
            "set_skill_preference",
            {
                "user_id": test_user_id,
                "agent_id": test_agent_id,
                "skill_name": skill1,
                "enabled": False,
            },
        )
        call_rpc(
            "set_skill_preference",
            {
                "user_id": test_user_id,
                "agent_id": test_agent_id,
                "skill_name": skill2,
                "enabled": False,
            },
        )

        # List only disabled skills
        disabled_result = call_rpc(
            "skills_list",
            {
                "user_id": test_user_id,
                "agent_id": test_agent_id,
                "enabled_only": False,
            },
        )
        disabled_skills = disabled_result["skills"]
        disabled_names = [s["name"] for s in disabled_skills]

        assert skill1 in disabled_names
        assert skill2 in disabled_names
        assert len(disabled_skills) >= 2

        # Cleanup
        call_rpc(
            "delete_skill_preference",
            {"user_id": test_user_id, "agent_id": test_agent_id, "skill_name": skill1},
        )
        call_rpc(
            "delete_skill_preference",
            {"user_id": test_user_id, "agent_id": test_agent_id, "skill_name": skill2},
        )

    def test_filter_enabled_skills(self, test_user_id: str, test_agent_id: str):
        """Test filter_enabled_skills method."""
        # Get available skills
        all_skills_result = call_rpc("skills_list", {})
        all_skills = all_skills_result["skills"]
        assert len(all_skills) >= 3

        skill_names = [s["name"] for s in all_skills[:3]]
        skill1, skill2, skill3 = skill_names

        # Disable skill1 and skill3
        call_rpc(
            "set_skill_preference",
            {
                "user_id": test_user_id,
                "agent_id": test_agent_id,
                "skill_name": skill1,
                "enabled": False,
            },
        )
        call_rpc(
            "set_skill_preference",
            {
                "user_id": test_user_id,
                "agent_id": test_agent_id,
                "skill_name": skill3,
                "enabled": False,
            },
        )

        # Filter skills
        result = call_rpc(
            "filter_enabled_skills",
            {
                "user_id": test_user_id,
                "agent_id": test_agent_id,
                "skill_names": skill_names,
            },
        )
        enabled_skills = result["enabled_skills"]

        # Only skill2 should be enabled
        assert skill2 in enabled_skills
        assert skill1 not in enabled_skills
        assert skill3 not in enabled_skills

        # Cleanup
        call_rpc(
            "delete_skill_preference",
            {"user_id": test_user_id, "agent_id": test_agent_id, "skill_name": skill1},
        )
        call_rpc(
            "delete_skill_preference",
            {"user_id": test_user_id, "agent_id": test_agent_id, "skill_name": skill3},
        )

    def test_full_workflow(self, test_user_id: str, test_agent_id: str):
        """Test complete workflow: register agent, set preferences, list filtered skills."""
        # Get available skills
        all_skills_result = call_rpc("skills_list", {})
        all_skills = all_skills_result["skills"]
        assert len(all_skills) >= 2

        skill1 = all_skills[0]["name"]
        skill2 = all_skills[1]["name"]

        # Step 1: Disable skill1 for the agent
        pref1 = call_rpc(
            "set_skill_preference",
            {
                "user_id": test_user_id,
                "agent_id": test_agent_id,
                "skill_name": skill1,
                "enabled": False,
                "reason": "Safety: prevent dangerous operations",
            },
        )
        assert pref1["enabled"] is False

        # Step 2: Enable skill2 explicitly
        pref2 = call_rpc(
            "set_skill_preference",
            {
                "user_id": test_user_id,
                "agent_id": test_agent_id,
                "skill_name": skill2,
                "enabled": True,
                "reason": "Granted for testing",
            },
        )
        assert pref2["enabled"] is True

        # Step 3: List only enabled skills
        enabled_result = call_rpc(
            "skills_list",
            {
                "user_id": test_user_id,
                "agent_id": test_agent_id,
                "enabled_only": True,
            },
        )
        enabled_names = [s["name"] for s in enabled_result["skills"]]
        assert skill1 not in enabled_names
        assert skill2 in enabled_names

        # Step 4: Verify with is_skill_enabled
        result1 = call_rpc(
            "is_skill_enabled",
            {
                "user_id": test_user_id,
                "agent_id": test_agent_id,
                "skill_name": skill1,
            },
        )
        assert result1["enabled"] is False

        result2 = call_rpc(
            "is_skill_enabled",
            {
                "user_id": test_user_id,
                "agent_id": test_agent_id,
                "skill_name": skill2,
            },
        )
        assert result2["enabled"] is True

        # Step 5: Delete preference and verify default behavior
        call_rpc(
            "delete_skill_preference",
            {
                "user_id": test_user_id,
                "agent_id": test_agent_id,
                "skill_name": skill1,
            },
        )

        result1_after_delete = call_rpc(
            "is_skill_enabled",
            {
                "user_id": test_user_id,
                "agent_id": test_agent_id,
                "skill_name": skill1,
            },
        )
        assert result1_after_delete["enabled"] is True  # Default: enabled

        # Cleanup
        call_rpc(
            "delete_skill_preference",
            {"user_id": test_user_id, "agent_id": test_agent_id, "skill_name": skill2},
        )


if __name__ == "__main__":
    # Allow running as script
    pytest.main([__file__, "-v", "-s"])
