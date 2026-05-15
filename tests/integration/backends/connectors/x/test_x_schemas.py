"""Tests for X (Twitter) connector schemas, traits, and capabilities. Phase 4 (#3148)."""

import pytest
from pydantic import ValidationError

from nexus.backends.connectors.base import ConfirmLevel, Reversibility
from nexus.backends.connectors.x.schemas import CreateTweetSchema, DeleteTweetSchema
from nexus.contracts.backend_features import BackendFeature

# ---------------------------------------------------------------------------
# CreateTweetSchema
# ---------------------------------------------------------------------------


class TestCreateTweetSchema:
    """Tests for CreateTweetSchema validation."""

    def test_valid_creation(self):
        schema = CreateTweetSchema(
            agent_intent="User asked to post a status update about the project",
            text="Hello from Nexus!",
            user_confirmed=True,
        )
        assert schema.text == "Hello from Nexus!"
        assert schema.agent_intent == "User asked to post a status update about the project"
        assert schema.reply_to is None
        assert schema.quote_tweet_id is None
        assert schema.user_confirmed is True

    def test_valid_reply(self):
        schema = CreateTweetSchema(
            agent_intent="User wants to reply to a tweet about AI",
            text="Great point!",
            reply_to="1234567890",
            user_confirmed=True,
        )
        assert schema.reply_to == "1234567890"

    def test_valid_quote_tweet(self):
        schema = CreateTweetSchema(
            agent_intent="User wants to quote-tweet a news article",
            text="Interesting read",
            quote_tweet_id="9876543210",
            user_confirmed=True,
        )
        assert schema.quote_tweet_id == "9876543210"

    def test_missing_text_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            CreateTweetSchema(
                agent_intent="User asked to post a status update",
            )
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("text",) for e in errors)

    def test_missing_agent_intent_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            CreateTweetSchema(text="Hello")
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("agent_intent",) for e in errors)

    def test_agent_intent_too_short_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            CreateTweetSchema(agent_intent="short", text="Hello")
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("agent_intent",) for e in errors)

    def test_text_exceeds_280_chars_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            CreateTweetSchema(
                agent_intent="User wants to post a long tweet about something",
                text="x" * 281,
            )
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("text",) for e in errors)

    def test_text_exactly_280_chars(self):
        schema = CreateTweetSchema(
            agent_intent="User wants to post a maximum-length tweet",
            text="x" * 280,
        )
        assert len(schema.text) == 280

    def test_empty_text_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            CreateTweetSchema(
                agent_intent="User wants to post an empty tweet",
                text="",
            )
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("text",) for e in errors)

    def test_user_confirmed_defaults_false(self):
        schema = CreateTweetSchema(
            agent_intent="User asked to post a status update about something",
            text="Hello!",
        )
        assert schema.user_confirmed is False


# ---------------------------------------------------------------------------
# DeleteTweetSchema
# ---------------------------------------------------------------------------


class TestDeleteTweetSchema:
    """Tests for DeleteTweetSchema validation."""

    def test_valid_deletion(self):
        schema = DeleteTweetSchema(
            agent_intent="User wants to delete an embarrassing tweet",
            tweet_id="1234567890",
            user_confirmed=True,
        )
        assert schema.tweet_id == "1234567890"
        assert schema.user_confirmed is True

    def test_missing_tweet_id_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            DeleteTweetSchema(
                agent_intent="User wants to delete a tweet they regret",
            )
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("tweet_id",) for e in errors)

    def test_missing_agent_intent_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            DeleteTweetSchema(tweet_id="1234567890")
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("agent_intent",) for e in errors)

    def test_user_confirmed_defaults_false(self):
        schema = DeleteTweetSchema(
            agent_intent="User wants to delete a tweet they regret",
            tweet_id="1234567890",
        )
        assert schema.user_confirmed is False


# ---------------------------------------------------------------------------
# PathXBackend class attributes
# ---------------------------------------------------------------------------


class TestXConnectorCapabilities:
    """Tests that PathXBackend has the expected class attributes."""

    def test_has_schemas(self):
        from nexus.backends.connectors.x.connector import PathXBackend

        assert "create_tweet" in PathXBackend.SCHEMAS
        assert "delete_tweet" in PathXBackend.SCHEMAS
        assert PathXBackend.SCHEMAS["create_tweet"] is CreateTweetSchema
        assert PathXBackend.SCHEMAS["delete_tweet"] is DeleteTweetSchema

    def test_has_operation_traits(self):
        from nexus.backends.connectors.x.connector import PathXBackend

        assert "create_tweet" in PathXBackend.OPERATION_TRAITS
        assert "delete_tweet" in PathXBackend.OPERATION_TRAITS

        create_traits = PathXBackend.OPERATION_TRAITS["create_tweet"]
        assert create_traits.reversibility == Reversibility.PARTIAL
        assert create_traits.confirm == ConfirmLevel.USER

        delete_traits = PathXBackend.OPERATION_TRAITS["delete_tweet"]
        assert delete_traits.reversibility == Reversibility.NONE
        assert delete_traits.confirm == ConfirmLevel.USER

    def test_has_error_registry(self):
        from nexus.backends.connectors.x.connector import PathXBackend

        assert "MISSING_AGENT_INTENT" in PathXBackend.ERROR_REGISTRY
        assert "TWEET_TOO_LONG" in PathXBackend.ERROR_REGISTRY

    def test_has_readme_doc_capability(self):
        from nexus.backends.connectors.x.connector import PathXBackend

        assert BackendFeature.README_DOC in PathXBackend._BACKEND_FEATURES

    def test_skill_name(self):
        from nexus.backends.connectors.x.connector import PathXBackend

        assert PathXBackend.SKILL_NAME == "x"

    def test_inherits_validated_mixin(self):
        from nexus.backends.connectors.base import TraitBasedMixin, ValidatedMixin
        from nexus.backends.connectors.x.connector import PathXBackend

        assert issubclass(PathXBackend, ValidatedMixin)
        assert issubclass(PathXBackend, TraitBasedMixin)
