"""Unit tests for skill user preferences."""

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from nexus.core.exceptions import ValidationError
from nexus.skills.preferences import SkillPreference, SkillPreferenceManager


@pytest.fixture
def mock_session():
    """Create a mock database session."""
    session = MagicMock()
    session.query = MagicMock()
    session.add = MagicMock()
    session.commit = MagicMock()
    session.delete = MagicMock()
    return session


@pytest.fixture
def preference_manager(mock_session):
    """Create a SkillPreferenceManager with mock session."""
    return SkillPreferenceManager(mock_session)


class TestSkillPreference:
    """Tests for SkillPreference dataclass."""

    def test_validate_success(self):
        """Test validation with valid data."""
        pref = SkillPreference(
            preference_id=str(uuid.uuid4()),
            user_id="alice",
            skill_name="test-skill",
            enabled=True,
        )
        pref.validate()  # Should not raise

    def test_validate_missing_user_id(self):
        """Test validation fails without user_id."""
        pref = SkillPreference(
            preference_id=str(uuid.uuid4()),
            user_id="",
            skill_name="test-skill",
            enabled=True,
        )
        with pytest.raises(ValidationError, match="user_id is required"):
            pref.validate()

    def test_validate_missing_skill_name(self):
        """Test validation fails without skill_name."""
        pref = SkillPreference(
            preference_id=str(uuid.uuid4()),
            user_id="alice",
            skill_name="",
            enabled=True,
        )
        with pytest.raises(ValidationError, match="skill_name is required"):
            pref.validate()

    def test_validate_missing_preference_id(self):
        """Test validation fails without preference_id."""
        pref = SkillPreference(
            preference_id="",
            user_id="alice",
            skill_name="test-skill",
            enabled=True,
        )
        with pytest.raises(ValidationError, match="preference_id is required"):
            pref.validate()


class TestSkillPreferenceManager:
    """Tests for SkillPreferenceManager."""

    def test_set_preference_new(self, preference_manager, mock_session):
        """Test setting a new preference."""
        # Mock no existing preference
        mock_query = MagicMock()
        mock_query.filter.return_value.filter.return_value.first.return_value = None
        mock_session.query.return_value = mock_query

        # Set preference
        pref = preference_manager.set_preference(
            user_id="alice",
            skill_name="test-skill",
            enabled=False,
            reason="Not needed",
        )

        # Verify
        assert pref.user_id == "alice"
        assert pref.skill_name == "test-skill"
        assert pref.enabled is False
        assert pref.reason == "Not needed"
        assert pref.agent_id is None
        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()

    def test_set_preference_update_existing(self, preference_manager, mock_session):
        """Test updating an existing preference."""
        # Mock existing preference
        existing = MagicMock()
        existing.preference_id = "existing-id"
        existing.user_id = "alice"
        existing.skill_name = "test-skill"
        existing.agent_id = None
        existing.tenant_id = None
        existing.enabled = 1
        existing.reason = "Old reason"
        existing.created_at = datetime.now(UTC)
        existing.updated_at = datetime.now(UTC)

        mock_query = MagicMock()
        mock_query.filter.return_value.filter.return_value.first.return_value = existing
        mock_session.query.return_value = mock_query

        # Update preference
        pref = preference_manager.set_preference(
            user_id="alice",
            skill_name="test-skill",
            enabled=False,
            reason="New reason",
        )

        # Verify
        assert pref.enabled is False
        assert existing.enabled == 0  # Updated in database
        assert existing.reason == "New reason"
        mock_session.add.assert_not_called()  # Not adding new record
        mock_session.commit.assert_called_once()

    def test_get_preference_found(self, preference_manager, mock_session):
        """Test getting an existing preference."""
        # Mock existing preference
        existing = MagicMock()
        existing.preference_id = "pref-id"
        existing.user_id = "alice"
        existing.skill_name = "test-skill"
        existing.agent_id = None
        existing.tenant_id = None
        existing.enabled = 0
        existing.reason = "Test reason"
        existing.created_at = datetime.now(UTC)
        existing.updated_at = datetime.now(UTC)

        mock_query = MagicMock()
        mock_query.filter.return_value.filter.return_value.first.return_value = existing
        mock_session.query.return_value = mock_query

        # Get preference
        pref = preference_manager.get_preference(
            user_id="alice",
            skill_name="test-skill",
        )

        # Verify
        assert pref is not None
        assert pref.user_id == "alice"
        assert pref.skill_name == "test-skill"
        assert pref.enabled is False  # 0 converted to False

    def test_get_preference_not_found(self, preference_manager, mock_session):
        """Test getting a non-existent preference."""
        # Mock no preference found
        mock_query = MagicMock()
        mock_query.filter.return_value.filter.return_value.first.return_value = None
        mock_session.query.return_value = mock_query

        # Get preference
        pref = preference_manager.get_preference(
            user_id="alice",
            skill_name="nonexistent",
        )

        # Verify
        assert pref is None

    def test_is_skill_enabled_default(self, preference_manager, mock_session):
        """Test that skills are enabled by default when no preference exists."""
        # Mock no preferences found
        mock_query = MagicMock()
        mock_query.filter.return_value.filter.return_value.first.return_value = None
        mock_session.query.return_value = mock_query

        # Check if enabled
        is_enabled = preference_manager.is_skill_enabled(
            user_id="alice",
            skill_name="test-skill",
        )

        # Verify default is True
        assert is_enabled is True

    def test_is_skill_enabled_user_level(self, preference_manager, mock_session):
        """Test checking skill enabled at user level."""
        # Mock user-level preference (disabled)
        user_pref = MagicMock()
        user_pref.enabled = 0

        mock_query = MagicMock()
        # First call (agent-specific): None
        # Second call (user-level): user_pref
        mock_query.filter.return_value.filter.return_value.first.side_effect = [None, user_pref]
        mock_session.query.return_value = mock_query

        # Check if enabled
        is_enabled = preference_manager.is_skill_enabled(
            user_id="alice",
            skill_name="test-skill",
            agent_id="bot",
        )

        # Verify uses user-level preference
        assert is_enabled is False

    def test_is_skill_enabled_agent_override(self, preference_manager, mock_session):
        """Test agent-level preference overrides user-level."""
        # Mock agent-level preference (enabled)
        agent_pref = MagicMock()
        agent_pref.enabled = 1

        mock_query = MagicMock()
        # First call (agent-specific): agent_pref
        mock_query.filter.return_value.filter.return_value.first.return_value = agent_pref
        mock_session.query.return_value = mock_query

        # Check if enabled
        is_enabled = preference_manager.is_skill_enabled(
            user_id="alice",
            skill_name="test-skill",
            agent_id="bot",
        )

        # Verify uses agent-level preference (doesn't check user-level)
        assert is_enabled is True

    def test_filter_enabled_skills(self, preference_manager):
        """Test filtering skills based on preferences."""

        # Mock is_skill_enabled to return specific results
        def mock_is_enabled(user_id, skill_name, agent_id=None, tenant_id=None):
            return skill_name in ["skill-a", "skill-c"]  # skill-b disabled

        preference_manager.is_skill_enabled = mock_is_enabled

        # Filter skills
        all_skills = ["skill-a", "skill-b", "skill-c"]
        enabled = preference_manager.filter_enabled_skills(
            user_id="alice",
            skill_names=all_skills,
        )

        # Verify
        assert enabled == ["skill-a", "skill-c"]

    def test_delete_preference_success(self, preference_manager, mock_session):
        """Test deleting an existing preference."""
        # Mock existing preference
        existing = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.filter.return_value.first.return_value = existing
        mock_session.query.return_value = mock_query

        # Delete preference
        deleted = preference_manager.delete_preference(
            user_id="alice",
            skill_name="test-skill",
        )

        # Verify
        assert deleted is True
        mock_session.delete.assert_called_once_with(existing)
        mock_session.commit.assert_called_once()

    def test_delete_preference_not_found(self, preference_manager, mock_session):
        """Test deleting a non-existent preference."""
        # Mock no preference found
        mock_query = MagicMock()
        mock_query.filter.return_value.filter.return_value.first.return_value = None
        mock_session.query.return_value = mock_query

        # Delete preference
        deleted = preference_manager.delete_preference(
            user_id="alice",
            skill_name="nonexistent",
        )

        # Verify
        assert deleted is False
        mock_session.delete.assert_not_called()

    def test_list_user_preferences(self, preference_manager, mock_session):
        """Test listing all preferences for a user."""
        # Mock preferences
        pref1 = MagicMock()
        pref1.preference_id = "id1"
        pref1.user_id = "alice"
        pref1.agent_id = None
        pref1.tenant_id = None
        pref1.skill_name = "skill-a"
        pref1.enabled = 1
        pref1.reason = None
        pref1.created_at = datetime.now(UTC)
        pref1.updated_at = datetime.now(UTC)

        pref2 = MagicMock()
        pref2.preference_id = "id2"
        pref2.user_id = "alice"
        pref2.agent_id = None
        pref2.tenant_id = None
        pref2.skill_name = "skill-b"
        pref2.enabled = 0
        pref2.reason = "Not needed"
        pref2.created_at = datetime.now(UTC)
        pref2.updated_at = datetime.now(UTC)

        mock_query = MagicMock()
        mock_query.filter.return_value.all.return_value = [pref1, pref2]
        mock_session.query.return_value = mock_query

        # List preferences
        prefs = preference_manager.list_user_preferences(user_id="alice")

        # Verify
        assert len(prefs) == 2
        assert prefs[0].skill_name == "skill-a"
        assert prefs[0].enabled is True
        assert prefs[1].skill_name == "skill-b"
        assert prefs[1].enabled is False

    def test_bulk_set_preferences(self, preference_manager):
        """Test bulk setting preferences."""
        # Mock set_preference to succeed
        preference_manager.set_preference = MagicMock(return_value=MagicMock())

        # Bulk set
        preferences = [
            ("skill-a", True),
            ("skill-b", False),
            ("skill-c", True),
        ]
        count = preference_manager.bulk_set_preferences(
            user_id="alice",
            preferences=preferences,
        )

        # Verify
        assert count == 3
        assert preference_manager.set_preference.call_count == 3
