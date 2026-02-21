"""Characterization tests for TraitBasedMixin.validate_traits().

Written BEFORE extracting shared error codes to base_errors.py (Issue #2086).
Locks down the validation behavior so extraction can be verified.
"""

import pytest

from nexus.backends.connectors.base import (
    ConfirmLevel,
    ErrorDef,
    OpTraits,
    Reversibility,
    TraitBasedMixin,
    ValidationError,
)


class FakeConnector(TraitBasedMixin):
    """Minimal connector with operation traits for testing."""

    OPERATION_TRAITS = {
        "create_event": OpTraits(
            reversibility=Reversibility.FULL,
            confirm=ConfirmLevel.INTENT,
            intent_min_length=10,
        ),
        "delete_event": OpTraits(
            reversibility=Reversibility.NONE,
            confirm=ConfirmLevel.EXPLICIT,
        ),
        "send_email": OpTraits(
            reversibility=Reversibility.NONE,
            confirm=ConfirmLevel.USER,
            warnings=["THIS ACTION CANNOT BE UNDONE"],
        ),
        "list_events": OpTraits(
            reversibility=Reversibility.FULL,
            confirm=ConfirmLevel.NONE,
        ),
    }
    ERROR_REGISTRY: dict[str, ErrorDef] = {
        "MISSING_AGENT_INTENT": ErrorDef(
            message="Operations require agent_intent",
            skill_section="required-format",
            fix_example="# agent_intent: reason",
        ),
        "AGENT_INTENT_TOO_SHORT": ErrorDef(
            message="agent_intent must be at least 10 characters",
            skill_section="required-format",
        ),
        "MISSING_CONFIRM": ErrorDef(
            message="Requires explicit confirmation",
            skill_section="delete-operation",
            fix_example="# confirm: true",
        ),
        "MISSING_USER_CONFIRMATION": ErrorDef(
            message="Requires user confirmation",
            skill_section="irreversible-operations",
        ),
    }
    SKILL_NAME = "test_connector"


@pytest.fixture()
def connector() -> FakeConnector:
    c = FakeConnector()
    c._mount_path = "/mnt/test"  # type: ignore[attr-defined]
    return c


class TestValidateTraitsNoTraits:
    def test_unknown_operation_returns_empty(self, connector: FakeConnector) -> None:
        result = connector.validate_traits("unknown_op", {})
        assert result == []


class TestValidateTraitsNoConfirmNeeded:
    def test_none_level_skips_all_checks(self, connector: FakeConnector) -> None:
        result = connector.validate_traits("list_events", {})
        assert result == []


class TestValidateTraitsMissingAgentIntent:
    def test_missing_intent_raises(self, connector: FakeConnector) -> None:
        with pytest.raises(ValidationError) as exc_info:
            connector.validate_traits("create_event", {})
        assert exc_info.value.code == "MISSING_AGENT_INTENT"

    def test_empty_intent_raises(self, connector: FakeConnector) -> None:
        with pytest.raises(ValidationError) as exc_info:
            connector.validate_traits("create_event", {"agent_intent": ""})
        assert exc_info.value.code == "MISSING_AGENT_INTENT"


class TestValidateTraitsIntentTooShort:
    def test_short_intent_raises(self, connector: FakeConnector) -> None:
        with pytest.raises(ValidationError) as exc_info:
            connector.validate_traits("create_event", {"agent_intent": "short"})
        assert exc_info.value.code == "AGENT_INTENT_TOO_SHORT"

    def test_exact_min_length_passes(self, connector: FakeConnector) -> None:
        result = connector.validate_traits("create_event", {"agent_intent": "a" * 10})
        assert result == []  # No warnings for create_event


class TestValidateTraitsMissingConfirm:
    def test_missing_confirm_raises(self, connector: FakeConnector) -> None:
        with pytest.raises(ValidationError) as exc_info:
            connector.validate_traits(
                "delete_event",
                {"agent_intent": "User wants to delete meeting"},
            )
        assert exc_info.value.code == "MISSING_CONFIRM"

    def test_confirm_true_passes(self, connector: FakeConnector) -> None:
        result = connector.validate_traits(
            "delete_event",
            {"agent_intent": "User wants to delete meeting", "confirm": True},
        )
        assert result == []


class TestValidateTraitsMissingUserConfirmation:
    def test_missing_user_confirmed_raises(self, connector: FakeConnector) -> None:
        with pytest.raises(ValidationError) as exc_info:
            connector.validate_traits(
                "send_email",
                {
                    "agent_intent": "User requested email send",
                    "confirm": True,
                },
            )
        assert exc_info.value.code == "MISSING_USER_CONFIRMATION"

    def test_user_confirmed_passes_with_warnings(self, connector: FakeConnector) -> None:
        result = connector.validate_traits(
            "send_email",
            {
                "agent_intent": "User requested email send",
                "confirm": True,
                "user_confirmed": True,
            },
        )
        assert "THIS ACTION CANNOT BE UNDONE" in result
