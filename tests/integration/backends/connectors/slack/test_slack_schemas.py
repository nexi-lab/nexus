"""Tests for Slack connector schemas, traits, and capabilities. Phase 4 (#3148)."""

import pytest
from pydantic import ValidationError

from nexus.backends.connectors.base import ConfirmLevel, Reversibility
from nexus.backends.connectors.slack.schemas import (
    DeleteMessageSchema,
    SendMessageSchema,
    UpdateMessageSchema,
)
from nexus.contracts.backend_features import BackendFeature

# ---------------------------------------------------------------------------
# SendMessageSchema
# ---------------------------------------------------------------------------


class TestSendMessageSchema:
    """Tests for SendMessageSchema validation."""

    def test_valid_message(self):
        schema = SendMessageSchema(
            agent_intent="User asked to send a project update to the team",
            channel="C01234ABCDE",
            text="Hello team!",
            user_confirmed=True,
        )
        assert schema.channel == "C01234ABCDE"
        assert schema.text == "Hello team!"
        assert schema.thread_ts is None
        assert schema.unfurl_links is True
        assert schema.user_confirmed is True

    def test_valid_threaded_reply(self):
        schema = SendMessageSchema(
            agent_intent="User wants to reply in a thread about the bug",
            channel="C01234ABCDE",
            text="I will look into this.",
            thread_ts="1234567890.123456",
            user_confirmed=True,
        )
        assert schema.thread_ts == "1234567890.123456"

    def test_missing_channel_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            SendMessageSchema(
                agent_intent="User asked to send a message about something",
                text="Hello!",
            )
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("channel",) for e in errors)

    def test_missing_text_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            SendMessageSchema(
                agent_intent="User asked to send a message about something",
                channel="C01234ABCDE",
            )
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("text",) for e in errors)

    def test_empty_text_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            SendMessageSchema(
                agent_intent="User asked to send an empty message for some reason",
                channel="C01234ABCDE",
                text="",
            )
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("text",) for e in errors)

    def test_missing_agent_intent_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            SendMessageSchema(
                channel="C01234ABCDE",
                text="Hello!",
            )
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("agent_intent",) for e in errors)

    def test_agent_intent_too_short_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            SendMessageSchema(
                agent_intent="short",
                channel="C01234ABCDE",
                text="Hello!",
            )
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("agent_intent",) for e in errors)

    def test_user_confirmed_defaults_false(self):
        schema = SendMessageSchema(
            agent_intent="User asked to send a message to the general channel",
            channel="C01234ABCDE",
            text="Hello!",
        )
        assert schema.user_confirmed is False

    def test_unfurl_links_defaults_true(self):
        schema = SendMessageSchema(
            agent_intent="User asked to share a link in the channel",
            channel="C01234ABCDE",
            text="Check this out: https://example.com",
        )
        assert schema.unfurl_links is True


# ---------------------------------------------------------------------------
# DeleteMessageSchema
# ---------------------------------------------------------------------------


class TestDeleteMessageSchema:
    """Tests for DeleteMessageSchema validation."""

    def test_valid_deletion(self):
        schema = DeleteMessageSchema(
            agent_intent="User wants to delete a message sent by mistake",
            channel="C01234ABCDE",
            ts="1234567890.123456",
            user_confirmed=True,
        )
        assert schema.channel == "C01234ABCDE"
        assert schema.ts == "1234567890.123456"
        assert schema.user_confirmed is True

    def test_missing_channel_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            DeleteMessageSchema(
                agent_intent="User wants to delete a message they regret",
                ts="1234567890.123456",
            )
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("channel",) for e in errors)

    def test_missing_ts_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            DeleteMessageSchema(
                agent_intent="User wants to delete a message they regret",
                channel="C01234ABCDE",
            )
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("ts",) for e in errors)

    def test_user_confirmed_defaults_false(self):
        schema = DeleteMessageSchema(
            agent_intent="User wants to delete a message they regret",
            channel="C01234ABCDE",
            ts="1234567890.123456",
        )
        assert schema.user_confirmed is False


# ---------------------------------------------------------------------------
# UpdateMessageSchema
# ---------------------------------------------------------------------------


class TestUpdateMessageSchema:
    """Tests for UpdateMessageSchema validation."""

    def test_valid_update(self):
        schema = UpdateMessageSchema(
            agent_intent="User wants to fix a typo in their message",
            channel="C01234ABCDE",
            ts="1234567890.123456",
            text="Updated message content",
            confirm=True,
        )
        assert schema.channel == "C01234ABCDE"
        assert schema.ts == "1234567890.123456"
        assert schema.text == "Updated message content"
        assert schema.confirm is True

    def test_missing_text_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            UpdateMessageSchema(
                agent_intent="User wants to fix a typo in their message",
                channel="C01234ABCDE",
                ts="1234567890.123456",
            )
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("text",) for e in errors)

    def test_empty_text_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            UpdateMessageSchema(
                agent_intent="User wants to replace their message with nothing",
                channel="C01234ABCDE",
                ts="1234567890.123456",
                text="",
            )
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("text",) for e in errors)

    def test_confirm_defaults_false(self):
        schema = UpdateMessageSchema(
            agent_intent="User wants to fix a typo in their message",
            channel="C01234ABCDE",
            ts="1234567890.123456",
            text="Fixed text",
        )
        assert schema.confirm is False


# ---------------------------------------------------------------------------
# SlackConnectorBackend class attributes
# ---------------------------------------------------------------------------


class TestSlackConnectorCapabilities:
    """Tests that SlackConnectorBackend has the expected class attributes."""

    def test_has_schemas(self):
        from nexus.backends.connectors.slack.connector import SlackConnectorBackend

        assert "send_message" in SlackConnectorBackend.SCHEMAS
        assert "delete_message" in SlackConnectorBackend.SCHEMAS
        assert "update_message" in SlackConnectorBackend.SCHEMAS
        assert SlackConnectorBackend.SCHEMAS["send_message"] is SendMessageSchema
        assert SlackConnectorBackend.SCHEMAS["delete_message"] is DeleteMessageSchema
        assert SlackConnectorBackend.SCHEMAS["update_message"] is UpdateMessageSchema

    def test_has_operation_traits(self):
        from nexus.backends.connectors.slack.connector import SlackConnectorBackend

        assert "send_message" in SlackConnectorBackend.OPERATION_TRAITS
        assert "delete_message" in SlackConnectorBackend.OPERATION_TRAITS
        assert "update_message" in SlackConnectorBackend.OPERATION_TRAITS

        send_traits = SlackConnectorBackend.OPERATION_TRAITS["send_message"]
        assert send_traits.reversibility == Reversibility.NONE
        assert send_traits.confirm == ConfirmLevel.USER

        delete_traits = SlackConnectorBackend.OPERATION_TRAITS["delete_message"]
        assert delete_traits.reversibility == Reversibility.NONE
        assert delete_traits.confirm == ConfirmLevel.USER

        update_traits = SlackConnectorBackend.OPERATION_TRAITS["update_message"]
        assert update_traits.reversibility == Reversibility.FULL
        assert update_traits.confirm == ConfirmLevel.EXPLICIT

    def test_has_error_registry(self):
        from nexus.backends.connectors.slack.connector import SlackConnectorBackend

        assert "MISSING_AGENT_INTENT" in SlackConnectorBackend.ERROR_REGISTRY
        assert "CHANNEL_NOT_FOUND" in SlackConnectorBackend.ERROR_REGISTRY
        assert "MESSAGE_NOT_FOUND" in SlackConnectorBackend.ERROR_REGISTRY

    def test_has_skill_doc_capability(self):
        from nexus.backends.connectors.slack.connector import SlackConnectorBackend

        assert BackendFeature.SKILL_DOC in SlackConnectorBackend._BACKEND_FEATURES

    def test_skill_name(self):
        from nexus.backends.connectors.slack.connector import SlackConnectorBackend

        assert SlackConnectorBackend.SKILL_NAME == "slack"

    def test_inherits_validated_mixin(self):
        from nexus.backends.connectors.base import TraitBasedMixin, ValidatedMixin
        from nexus.backends.connectors.slack.connector import SlackConnectorBackend

        assert issubclass(SlackConnectorBackend, ValidatedMixin)
        assert issubclass(SlackConnectorBackend, TraitBasedMixin)
