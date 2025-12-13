"""Unit tests for skill preferences RPC methods."""

from unittest.mock import MagicMock, patch

import pytest

from nexus.core.nexus_fs import NexusFS


@pytest.fixture
def mock_nexus_fs():
    """Create a mock NexusFS instance."""
    nx = MagicMock(spec=NexusFS)
    nx.metadata = MagicMock()
    nx.metadata.SessionLocal = MagicMock()
    # Create a context manager mock for session
    session_mock = MagicMock()
    session_mock.__enter__ = MagicMock(return_value=session_mock)
    session_mock.__exit__ = MagicMock(return_value=False)
    nx.metadata.SessionLocal.return_value = session_mock
    return nx


class TestSkillPreferencesRPC:
    """Tests for skill preferences RPC methods."""

    def test_set_skill_preference(self, mock_nexus_fs):
        """Test set_skill_preference RPC method."""
        from nexus.skills import SkillPreference

        # Mock preference manager
        mock_pref = SkillPreference(
            preference_id="pref123",
            user_id="alice",
            agent_id="chatbot",
            skill_name="sql-query",
            enabled=False,
            reason="Safety",
            tenant_id=None,
            created_at=None,
            updated_at=None,
        )

        with patch("nexus.skills.SkillPreferenceManager") as MockPrefMgr:
            mock_mgr = MockPrefMgr.return_value
            mock_mgr.set_preference.return_value = mock_pref

            # Import and call the method
            from nexus.core.nexus_fs_skills import NexusFSSkillsMixin

            mixin = NexusFSSkillsMixin()
            mixin.metadata = mock_nexus_fs.metadata

            result = mixin.set_skill_preference(
                user_id="alice",
                agent_id="chatbot",
                skill_name="sql-query",
                enabled=False,
                reason="Safety",
            )

            # Verify
            assert result["user_id"] == "alice"
            assert result["agent_id"] == "chatbot"
            assert result["skill_name"] == "sql-query"
            assert result["enabled"] is False
            assert result["reason"] == "Safety"

            mock_mgr.set_preference.assert_called_once_with(
                user_id="alice",
                agent_id="chatbot",
                skill_name="sql-query",
                enabled=False,
                tenant_id=None,
                reason="Safety",
            )

    def test_get_skill_preference(self, mock_nexus_fs):
        """Test get_skill_preference RPC method."""
        from nexus.skills import SkillPreference

        mock_pref = SkillPreference(
            preference_id="pref123",
            user_id="alice",
            agent_id="chatbot",
            skill_name="sql-query",
            enabled=False,
            tenant_id=None,
            created_at=None,
            updated_at=None,
        )

        with patch("nexus.skills.SkillPreferenceManager") as MockPrefMgr:
            mock_mgr = MockPrefMgr.return_value
            mock_mgr.get_preference.return_value = mock_pref

            from nexus.core.nexus_fs_skills import NexusFSSkillsMixin

            mixin = NexusFSSkillsMixin()
            mixin.metadata = mock_nexus_fs.metadata

            result = mixin.get_skill_preference(
                user_id="alice",
                agent_id="chatbot",
                skill_name="sql-query",
            )

            assert result["preference"]["user_id"] == "alice"
            assert result["preference"]["agent_id"] == "chatbot"
            assert result["preference"]["skill_name"] == "sql-query"
            assert result["preference"]["enabled"] is False

    def test_get_skill_preference_not_found(self, mock_nexus_fs):
        """Test get_skill_preference when preference doesn't exist."""
        with patch("nexus.skills.SkillPreferenceManager") as MockPrefMgr:
            mock_mgr = MockPrefMgr.return_value
            mock_mgr.get_preference.return_value = None

            from nexus.core.nexus_fs_skills import NexusFSSkillsMixin

            mixin = NexusFSSkillsMixin()
            mixin.metadata = mock_nexus_fs.metadata

            result = mixin.get_skill_preference(
                user_id="alice",
                agent_id="chatbot",
                skill_name="nonexistent",
            )

            assert result["preference"] is None

    def test_is_skill_enabled(self, mock_nexus_fs):
        """Test is_skill_enabled RPC method."""
        with patch("nexus.skills.SkillPreferenceManager") as MockPrefMgr:
            mock_mgr = MockPrefMgr.return_value
            mock_mgr.is_skill_enabled.return_value = False

            from nexus.core.nexus_fs_skills import NexusFSSkillsMixin

            mixin = NexusFSSkillsMixin()
            mixin.metadata = mock_nexus_fs.metadata

            result = mixin.is_skill_enabled(
                user_id="alice",
                agent_id="chatbot",
                skill_name="sql-query",
            )

            assert result["enabled"] is False

            mock_mgr.is_skill_enabled.assert_called_once_with(
                user_id="alice",
                agent_id="chatbot",
                skill_name="sql-query",
                tenant_id=None,
            )

    def test_is_skill_enabled_default(self, mock_nexus_fs):
        """Test is_skill_enabled returns True by default."""
        with patch("nexus.skills.SkillPreferenceManager") as MockPrefMgr:
            mock_mgr = MockPrefMgr.return_value
            mock_mgr.is_skill_enabled.return_value = True

            from nexus.core.nexus_fs_skills import NexusFSSkillsMixin

            mixin = NexusFSSkillsMixin()
            mixin.metadata = mock_nexus_fs.metadata

            result = mixin.is_skill_enabled(
                user_id="alice",
                agent_id="dev-assistant",
                skill_name="code-review",
            )

            assert result["enabled"] is True

    def test_list_skill_preferences(self, mock_nexus_fs):
        """Test list_skill_preferences RPC method."""
        from nexus.skills import SkillPreference

        mock_prefs = [
            SkillPreference(
                preference_id="pref1",
                user_id="alice",
                agent_id="chatbot",
                skill_name="sql-query",
                enabled=False,
                tenant_id=None,
                created_at=None,
                updated_at=None,
            ),
            SkillPreference(
                preference_id="pref2",
                user_id="alice",
                agent_id="chatbot",
                skill_name="system-exec",
                enabled=False,
                tenant_id=None,
                created_at=None,
                updated_at=None,
            ),
        ]

        with patch("nexus.skills.SkillPreferenceManager") as MockPrefMgr:
            mock_mgr = MockPrefMgr.return_value
            mock_mgr.list_user_preferences.return_value = mock_prefs

            from nexus.core.nexus_fs_skills import NexusFSSkillsMixin

            mixin = NexusFSSkillsMixin()
            mixin.metadata = mock_nexus_fs.metadata

            result = mixin.list_skill_preferences(user_id="alice")

            assert len(result["preferences"]) == 2
            assert result["preferences"][0]["skill_name"] == "sql-query"
            assert result["preferences"][1]["skill_name"] == "system-exec"

    def test_list_skill_preferences_with_agent_id(self, mock_nexus_fs):
        """Test list_skill_preferences with agent_id filter."""
        from nexus.skills import SkillPreference

        mock_prefs = [
            SkillPreference(
                preference_id="pref1",
                user_id="alice",
                agent_id="chatbot",
                skill_name="sql-query",
                enabled=False,
                tenant_id=None,
                created_at=None,
                updated_at=None,
            ),
        ]

        with patch("nexus.skills.SkillPreferenceManager") as MockPrefMgr:
            mock_mgr = MockPrefMgr.return_value
            mock_mgr.list_user_preferences.return_value = mock_prefs

            from nexus.core.nexus_fs_skills import NexusFSSkillsMixin

            mixin = NexusFSSkillsMixin()
            mixin.metadata = mock_nexus_fs.metadata

            result = mixin.list_skill_preferences(user_id="alice", agent_id="chatbot")

            assert len(result["preferences"]) == 1
            mock_mgr.list_user_preferences.assert_called_once_with(
                user_id="alice",
                agent_id="chatbot",
                enabled_only=None,
            )

    def test_list_skill_preferences_enabled_only(self, mock_nexus_fs):
        """Test list_skill_preferences with enabled_only filter."""
        from nexus.skills import SkillPreference

        mock_prefs = [
            SkillPreference(
                preference_id="pref1",
                user_id="alice",
                agent_id="chatbot",
                skill_name="code-review",
                enabled=True,
                tenant_id=None,
                created_at=None,
                updated_at=None,
            ),
        ]

        with patch("nexus.skills.SkillPreferenceManager") as MockPrefMgr:
            mock_mgr = MockPrefMgr.return_value
            mock_mgr.list_user_preferences.return_value = mock_prefs

            from nexus.core.nexus_fs_skills import NexusFSSkillsMixin

            mixin = NexusFSSkillsMixin()
            mixin.metadata = mock_nexus_fs.metadata

            result = mixin.list_skill_preferences(user_id="alice", enabled_only=True)

            assert len(result["preferences"]) == 1
            assert result["preferences"][0]["enabled"] is True
            mock_mgr.list_user_preferences.assert_called_once_with(
                user_id="alice",
                agent_id=None,
                enabled_only=True,
            )

    def test_delete_skill_preference(self, mock_nexus_fs):
        """Test delete_skill_preference RPC method."""
        with patch("nexus.skills.SkillPreferenceManager") as MockPrefMgr:
            mock_mgr = MockPrefMgr.return_value
            mock_mgr.delete_preference.return_value = True

            from nexus.core.nexus_fs_skills import NexusFSSkillsMixin

            mixin = NexusFSSkillsMixin()
            mixin.metadata = mock_nexus_fs.metadata

            result = mixin.delete_skill_preference(
                user_id="alice",
                agent_id="chatbot",
                skill_name="sql-query",
            )

            assert result["success"] is True
            assert result["deleted"] is True

            mock_mgr.delete_preference.assert_called_once_with(
                user_id="alice",
                agent_id="chatbot",
                skill_name="sql-query",
            )

    def test_delete_skill_preference_not_found(self, mock_nexus_fs):
        """Test delete_skill_preference when preference doesn't exist."""
        with patch("nexus.skills.SkillPreferenceManager") as MockPrefMgr:
            mock_mgr = MockPrefMgr.return_value
            mock_mgr.delete_preference.return_value = False

            from nexus.core.nexus_fs_skills import NexusFSSkillsMixin

            mixin = NexusFSSkillsMixin()
            mixin.metadata = mock_nexus_fs.metadata

            result = mixin.delete_skill_preference(
                user_id="alice",
                agent_id="chatbot",
                skill_name="nonexistent",
            )

            assert result["success"] is False
            assert result["deleted"] is False

    def test_filter_enabled_skills(self, mock_nexus_fs):
        """Test filter_enabled_skills RPC method."""
        with patch("nexus.skills.SkillPreferenceManager") as MockPrefMgr:
            mock_mgr = MockPrefMgr.return_value
            # sql-query is revoked
            mock_mgr.filter_enabled_skills.return_value = ["code-review", "test-gen"]

            from nexus.core.nexus_fs_skills import NexusFSSkillsMixin

            mixin = NexusFSSkillsMixin()
            mixin.metadata = mock_nexus_fs.metadata

            result = mixin.filter_enabled_skills(
                user_id="alice",
                agent_id="chatbot",
                skill_names=["code-review", "sql-query", "test-gen"],
            )

            assert result["enabled_skills"] == ["code-review", "test-gen"]

            mock_mgr.filter_enabled_skills.assert_called_once_with(
                user_id="alice",
                agent_id="chatbot",
                skill_names=["code-review", "sql-query", "test-gen"],
                tenant_id=None,
            )
