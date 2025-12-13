"""Integration tests for skill preferences RPC API."""

import pytest

from nexus import connect


@pytest.fixture
def nx():
    """Create Nexus connection."""
    return connect()


class TestSkillPreferencesRPC:
    """Tests for skill preferences RPC methods."""

    def test_set_skill_preference(self, nx):
        """Test setting a skill preference via NexusFS."""
        # Set preference
        result = nx.set_skill_preference(
            user_id="test_user",
            agent_id="test_agent",
            skill_name="test_skill",
            enabled=False,
            reason="Test revoke",
        )

        assert result["user_id"] == "test_user"
        assert result["agent_id"] == "test_agent"
        assert result["skill_name"] == "test_skill"
        assert result["enabled"] is False
        assert result["reason"] == "Test revoke"

        # Cleanup
        nx.delete_skill_preference("test_user", "test_agent", "test_skill")

    def test_get_skill_preference(self, nx):
        """Test getting a skill preference."""
        # Create preference
        nx.set_skill_preference(
            user_id="test_user",
            agent_id="test_agent",
            skill_name="test_skill",
            enabled=False,
        )

        # Get preference
        result = nx.get_skill_preference("test_user", "test_agent", "test_skill")

        assert result["preference"] is not None
        assert result["preference"]["enabled"] is False

        # Cleanup
        nx.delete_skill_preference("test_user", "test_agent", "test_skill")

    def test_get_nonexistent_preference(self, nx):
        """Test getting a non-existent preference."""
        result = nx.get_skill_preference(
            "nonexistent_user", "nonexistent_agent", "nonexistent_skill"
        )

        assert result["preference"] is None

    def test_is_skill_enabled_default(self, nx):
        """Test that skills are enabled by default."""
        # Check non-existent preference (should default to enabled)
        result = nx.is_skill_enabled("test_user", "test_agent", "some_skill")

        assert result["enabled"] is True

    def test_is_skill_enabled_revoked(self, nx):
        """Test checking if a revoked skill is enabled."""
        # Revoke skill
        nx.set_skill_preference(
            user_id="test_user",
            agent_id="test_agent",
            skill_name="revoked_skill",
            enabled=False,
        )

        # Check if enabled
        result = nx.is_skill_enabled("test_user", "test_agent", "revoked_skill")

        assert result["enabled"] is False

        # Cleanup
        nx.delete_skill_preference("test_user", "test_agent", "revoked_skill")

    def test_list_skill_preferences(self, nx):
        """Test listing skill preferences."""
        # Create multiple preferences
        nx.set_skill_preference("test_user", "agent1", "skill1", False)
        nx.set_skill_preference("test_user", "agent1", "skill2", True)
        nx.set_skill_preference("test_user", "agent2", "skill1", False)

        # List all preferences for user
        result = nx.list_skill_preferences("test_user")
        prefs = result["preferences"]

        assert len(prefs) >= 3

        # List preferences for specific agent
        result = nx.list_skill_preferences("test_user", agent_id="agent1")
        agent1_prefs = result["preferences"]
        assert len(agent1_prefs) >= 2

        # List only enabled skills
        result = nx.list_skill_preferences("test_user", enabled_only=True)
        enabled_prefs = result["preferences"]
        assert all(p["enabled"] for p in enabled_prefs)

        # List only disabled skills
        result = nx.list_skill_preferences("test_user", enabled_only=False)
        disabled_prefs = result["preferences"]
        assert all(not p["enabled"] for p in disabled_prefs)

        # Cleanup
        nx.delete_skill_preference("test_user", "agent1", "skill1")
        nx.delete_skill_preference("test_user", "agent1", "skill2")
        nx.delete_skill_preference("test_user", "agent2", "skill1")

    def test_delete_skill_preference(self, nx):
        """Test deleting a skill preference."""
        # Create preference
        nx.set_skill_preference("test_user", "test_agent", "test_skill", False)

        # Delete preference
        result = nx.delete_skill_preference("test_user", "test_agent", "test_skill")

        assert result["deleted"] is True

        # Verify deleted
        result = nx.get_skill_preference("test_user", "test_agent", "test_skill")
        assert result["preference"] is None

        # Try deleting again (should return False)
        result = nx.delete_skill_preference("test_user", "test_agent", "test_skill")
        assert result["deleted"] is False

    def test_filter_enabled_skills(self, nx):
        """Test filtering skills by agent access."""
        # Revoke some skills
        nx.set_skill_preference("test_user", "test_agent", "skill1", False)
        nx.set_skill_preference("test_user", "test_agent", "skill3", False)

        # Filter skills
        all_skills = ["skill1", "skill2", "skill3", "skill4"]
        result = nx.filter_enabled_skills("test_user", "test_agent", all_skills)
        enabled_skills = result["enabled_skills"]

        # Only skill2 and skill4 should be enabled (not revoked)
        assert set(enabled_skills) == {"skill2", "skill4"}

        # Cleanup
        nx.delete_skill_preference("test_user", "test_agent", "skill1")
        nx.delete_skill_preference("test_user", "test_agent", "skill3")

    def test_update_existing_preference(self, nx):
        """Test updating an existing preference."""
        # Create preference (revoked)
        result1 = nx.set_skill_preference(
            user_id="test_user",
            agent_id="test_agent",
            skill_name="test_skill",
            enabled=False,
            reason="Initial revoke",
        )

        # Update preference (grant)
        result2 = nx.set_skill_preference(
            user_id="test_user",
            agent_id="test_agent",
            skill_name="test_skill",
            enabled=True,
            reason="Grant back",
        )

        # Should be same record
        assert result1["preference_id"] == result2["preference_id"]
        assert result2["enabled"] is True
        assert result2["reason"] == "Grant back"

        # Cleanup
        nx.delete_skill_preference("test_user", "test_agent", "test_skill")

    def test_agent_isolation(self, nx):
        """Test that preferences are isolated by agent."""
        # Revoke skill for agent1
        nx.set_skill_preference("test_user", "agent1", "skill1", False)

        # Check agent1 (revoked)
        result = nx.is_skill_enabled("test_user", "agent1", "skill1")
        assert result["enabled"] is False

        # Check agent2 (not revoked, default granted)
        result = nx.is_skill_enabled("test_user", "agent2", "skill1")
        assert result["enabled"] is True

        # Cleanup
        nx.delete_skill_preference("test_user", "agent1", "skill1")

    def test_safety_use_case(self, nx):
        """Test real-world safety use case: revoke dangerous skills from chatbot."""
        # Revoke dangerous skills from chatbot
        dangerous_skills = ["sql-query", "system-exec", "file-delete"]

        for skill in dangerous_skills:
            nx.set_skill_preference(
                user_id="alice",
                agent_id="chatbot",
                skill_name=skill,
                enabled=False,
                reason="Safety: prevent dangerous operations",
            )

        # Check that chatbot cannot use dangerous skills
        for skill in dangerous_skills:
            result = nx.is_skill_enabled("alice", "chatbot", skill)
            assert result["enabled"] is False

        # Check that dev-assistant can still use them (not revoked)
        for skill in dangerous_skills:
            result = nx.is_skill_enabled("alice", "dev-assistant", skill)
            assert result["enabled"] is True

        # Cleanup
        for skill in dangerous_skills:
            nx.delete_skill_preference("alice", "chatbot", skill)

    def test_list_with_agent_id_and_enabled_only(self, nx):
        """Test list_skill_preferences with both agent_id and enabled_only filters."""
        # Create preferences for different agents
        nx.set_skill_preference("test_user", "agent1", "skill1", False)
        nx.set_skill_preference("test_user", "agent1", "skill2", True)
        nx.set_skill_preference("test_user", "agent2", "skill1", False)

        # List only enabled skills for agent1
        result = nx.list_skill_preferences("test_user", agent_id="agent1", enabled_only=True)
        enabled_prefs = result["preferences"]
        assert len(enabled_prefs) >= 1
        assert all(p["enabled"] for p in enabled_prefs)
        assert all(p["agent_id"] == "agent1" for p in enabled_prefs)

        # Cleanup
        nx.delete_skill_preference("test_user", "agent1", "skill1")
        nx.delete_skill_preference("test_user", "agent1", "skill2")
        nx.delete_skill_preference("test_user", "agent2", "skill1")

    def test_skills_list_with_preference_filtering(self, nx):
        """Test skills_list filtered by preferences."""
        # Get all skills first
        all_skills_result = nx.skills_list()
        all_skills = all_skills_result["skills"]
        assert len(all_skills) > 0

        # Pick a skill to disable
        test_skill = all_skills[0]["name"]

        # Disable the skill for test_user/test_agent
        nx.set_skill_preference("test_user", "test_agent", test_skill, False)

        # List only enabled skills
        enabled_result = nx.skills_list(
            user_id="test_user", agent_id="test_agent", enabled_only=True
        )
        enabled_skills = enabled_result["skills"]
        enabled_names = [s["name"] for s in enabled_skills]

        # Verify test_skill is not in enabled list
        assert test_skill not in enabled_names
        assert len(enabled_skills) < len(all_skills)

        # List all skills with enabled flags
        all_with_flags_result = nx.skills_list(
            user_id="test_user", agent_id="test_agent", enabled_only=None
        )
        all_with_flags = all_with_flags_result["skills"]

        # Find test_skill and verify it's marked as disabled
        test_skill_data = next(s for s in all_with_flags if s["name"] == test_skill)
        assert test_skill_data["enabled"] is False

        # Verify other skills are marked as enabled
        other_skills = [s for s in all_with_flags if s["name"] != test_skill]
        assert all(s.get("enabled", True) for s in other_skills)

        # Cleanup
        nx.delete_skill_preference("test_user", "test_agent", test_skill)

    def test_skills_list_without_preferences(self, nx):
        """Test skills_list without preference filtering returns all skills."""
        # List all skills without preferences
        result = nx.skills_list()
        all_skills = result["skills"]
        assert len(all_skills) > 0

        # List with user_id/agent_id but no preferences set (all should be enabled)
        result_with_prefs = nx.skills_list(
            user_id="test_user", agent_id="test_agent", enabled_only=True
        )
        enabled_skills = result_with_prefs["skills"]

        # All skills should be enabled by default
        assert len(enabled_skills) == len(all_skills)

    def test_skills_list_disabled_only(self, nx):
        """Test skills_list with enabled_only=False returns only disabled skills."""
        # Get all skills
        all_skills_result = nx.skills_list()
        all_skills = all_skills_result["skills"]
        assert len(all_skills) >= 2

        # Disable two skills
        skill1 = all_skills[0]["name"]
        skill2 = all_skills[1]["name"]

        nx.set_skill_preference("test_user", "test_agent", skill1, False)
        nx.set_skill_preference("test_user", "test_agent", skill2, False)

        # List only disabled skills
        disabled_result = nx.skills_list(
            user_id="test_user", agent_id="test_agent", enabled_only=False
        )
        disabled_skills = disabled_result["skills"]
        disabled_names = [s["name"] for s in disabled_skills]

        assert skill1 in disabled_names
        assert skill2 in disabled_names
        assert len(disabled_skills) == 2

        # Cleanup
        nx.delete_skill_preference("test_user", "test_agent", skill1)
        nx.delete_skill_preference("test_user", "test_agent", skill2)
