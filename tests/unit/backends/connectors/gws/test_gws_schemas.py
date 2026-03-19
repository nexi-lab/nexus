"""Tests for Google Workspace CLI connector schemas and YAML configs (Phase 3, Issue #3148).

Covers:
- Valid schema construction for all 6 schemas (Sheets, Docs, Chat)
- Missing required fields rejected via model_validate
- Too-short agent_intent rejected
- Default values applied correctly
- YAML configs load and validate via load_connector_config
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from nexus.backends.connectors.cli.loader import load_connector_config
from nexus.backends.connectors.gws.schemas import (
    AppendRowsSchema,
    CreateSpaceSchema,
    InsertTextSchema,
    ReplaceTextSchema,
    SendMessageSchema,
    UpdateCellsSchema,
)

# Path to the YAML config directory
CONFIGS_DIR = (
    Path(__file__).resolve().parents[5]
    / "src"
    / "nexus"
    / "backends"
    / "connectors"
    / "gws"
    / "configs"
)


# ---------------------------------------------------------------------------
# Google Sheets — AppendRowsSchema
# ---------------------------------------------------------------------------


class TestAppendRowsSchema:
    def test_valid_append(self) -> None:
        schema = AppendRowsSchema(
            agent_intent="Appending quarterly revenue data from report",
            spreadsheet_id="1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
            sheet_name="Q1",
            values=[["Revenue", "1000"], ["Costs", "500"]],
        )
        assert schema.spreadsheet_id.startswith("1Bxi")
        assert schema.sheet_name == "Q1"
        assert len(schema.values) == 2
        assert schema.value_input_option == "USER_ENTERED"
        assert schema.confirm is False

    def test_defaults(self) -> None:
        schema = AppendRowsSchema(
            agent_intent="Adding new rows to the default sheet tab",
            spreadsheet_id="abc123",
            values=[["a", "b"]],
        )
        assert schema.sheet_name == "Sheet1"
        assert schema.value_input_option == "USER_ENTERED"

    def test_missing_spreadsheet_id(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AppendRowsSchema.model_validate(
                {"agent_intent": "Valid intent for this operation", "values": [["a"]]}
            )
        assert "spreadsheet_id" in str(exc_info.value)

    def test_missing_values(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            AppendRowsSchema.model_validate(
                {
                    "agent_intent": "Valid intent for this operation",
                    "spreadsheet_id": "abc123",
                }
            )
        assert "values" in str(exc_info.value)

    def test_empty_values_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AppendRowsSchema(
                agent_intent="Valid intent for this operation",
                spreadsheet_id="abc123",
                values=[],
            )

    def test_short_intent_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AppendRowsSchema(
                agent_intent="too short",
                spreadsheet_id="abc123",
                values=[["a"]],
            )


# ---------------------------------------------------------------------------
# Google Sheets — UpdateCellsSchema
# ---------------------------------------------------------------------------


class TestUpdateCellsSchema:
    def test_valid_update(self) -> None:
        schema = UpdateCellsSchema(
            agent_intent="Updating status column for completed tasks",
            spreadsheet_id="abc123",
            range="Sheet1!A1:B2",
            values=[["Done", "100%"], ["Pending", "50%"]],
        )
        assert schema.range == "Sheet1!A1:B2"
        assert schema.confirm is False

    def test_missing_range(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            UpdateCellsSchema.model_validate(
                {
                    "agent_intent": "Valid intent for this operation",
                    "spreadsheet_id": "abc123",
                    "values": [["a"]],
                }
            )
        assert "range" in str(exc_info.value)

    def test_short_intent_rejected(self) -> None:
        with pytest.raises(ValidationError):
            UpdateCellsSchema(
                agent_intent="short",
                spreadsheet_id="abc123",
                range="A1:B2",
                values=[["a"]],
            )


# ---------------------------------------------------------------------------
# Google Docs — InsertTextSchema
# ---------------------------------------------------------------------------


class TestInsertTextSchema:
    def test_valid_insert(self) -> None:
        schema = InsertTextSchema(
            agent_intent="Inserting meeting notes at the end of the document",
            document_id="doc123",
            text="Meeting notes for 2024-01-15",
        )
        assert schema.location == "end"
        assert schema.confirm is False

    def test_custom_location(self) -> None:
        schema = InsertTextSchema(
            agent_intent="Inserting header at the start of document",
            document_id="doc123",
            text="# Header",
            location="start",
        )
        assert schema.location == "start"

    def test_missing_text(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            InsertTextSchema.model_validate(
                {
                    "agent_intent": "Valid intent for this operation",
                    "document_id": "doc123",
                }
            )
        assert "text" in str(exc_info.value)

    def test_empty_text_rejected(self) -> None:
        with pytest.raises(ValidationError):
            InsertTextSchema(
                agent_intent="Valid intent for this operation",
                document_id="doc123",
                text="",
            )

    def test_short_intent_rejected(self) -> None:
        with pytest.raises(ValidationError):
            InsertTextSchema(
                agent_intent="short",
                document_id="doc123",
                text="some text",
            )


# ---------------------------------------------------------------------------
# Google Docs — ReplaceTextSchema
# ---------------------------------------------------------------------------


class TestReplaceTextSchema:
    def test_valid_replace(self) -> None:
        schema = ReplaceTextSchema(
            agent_intent="Replacing placeholder with actual company name",
            document_id="doc123",
            find="{{COMPANY}}",
            replace="Acme Corp",
        )
        assert schema.match_case is True
        assert schema.confirm is False

    def test_replace_with_empty_string(self) -> None:
        """Replacing with empty string is valid (deletion)."""
        schema = ReplaceTextSchema(
            agent_intent="Removing deprecated disclaimer text from document",
            document_id="doc123",
            find="DEPRECATED",
            replace="",
        )
        assert schema.replace == ""

    def test_missing_find(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ReplaceTextSchema.model_validate(
                {
                    "agent_intent": "Valid intent for this operation",
                    "document_id": "doc123",
                    "replace": "new",
                }
            )
        assert "find" in str(exc_info.value)

    def test_empty_find_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ReplaceTextSchema(
                agent_intent="Valid intent for this operation",
                document_id="doc123",
                find="",
                replace="new",
            )

    def test_case_insensitive(self) -> None:
        schema = ReplaceTextSchema(
            agent_intent="Replacing all variations of the old term",
            document_id="doc123",
            find="old",
            replace="new",
            match_case=False,
        )
        assert schema.match_case is False


# ---------------------------------------------------------------------------
# Google Chat — SendMessageSchema
# ---------------------------------------------------------------------------


class TestSendMessageSchema:
    def test_valid_message(self) -> None:
        schema = SendMessageSchema(
            agent_intent="Sending daily standup summary to team space",
            space="spaces/AAAA_BBBB",
            text="Good morning! Here is the standup summary.",
        )
        assert schema.thread_key is None
        assert schema.user_confirmed is False

    def test_threaded_reply(self) -> None:
        schema = SendMessageSchema(
            agent_intent="Replying to deployment notification thread",
            space="spaces/AAAA_BBBB",
            text="Deployment complete.",
            thread_key="deploy-2024-01-15",
        )
        assert schema.thread_key == "deploy-2024-01-15"

    def test_missing_space(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            SendMessageSchema.model_validate(
                {
                    "agent_intent": "Valid intent for this operation",
                    "text": "hello",
                }
            )
        assert "space" in str(exc_info.value)

    def test_empty_text_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SendMessageSchema(
                agent_intent="Valid intent for this operation",
                space="spaces/AAAA_BBBB",
                text="",
            )

    def test_short_intent_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SendMessageSchema(
                agent_intent="short",
                space="spaces/AAAA_BBBB",
                text="hello",
            )


# ---------------------------------------------------------------------------
# Google Chat — CreateSpaceSchema
# ---------------------------------------------------------------------------


class TestCreateSpaceSchema:
    def test_valid_space(self) -> None:
        schema = CreateSpaceSchema(
            agent_intent="Creating a project discussion space for Q1 planning",
            display_name="Q1 Planning",
        )
        assert schema.space_type == "SPACE"
        assert schema.confirm is False

    def test_group_chat_type(self) -> None:
        schema = CreateSpaceSchema(
            agent_intent="Creating a group chat for the design review team",
            display_name="Design Review",
            space_type="GROUP_CHAT",
        )
        assert schema.space_type == "GROUP_CHAT"

    def test_missing_display_name(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            CreateSpaceSchema.model_validate({"agent_intent": "Valid intent for this operation"})
        assert "display_name" in str(exc_info.value)

    def test_empty_display_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CreateSpaceSchema(
                agent_intent="Valid intent for this operation",
                display_name="",
            )

    def test_display_name_too_long(self) -> None:
        with pytest.raises(ValidationError):
            CreateSpaceSchema(
                agent_intent="Valid intent for this operation",
                display_name="x" * 129,
            )

    def test_short_intent_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CreateSpaceSchema(
                agent_intent="short",
                display_name="My Space",
            )


# ---------------------------------------------------------------------------
# YAML config loading
# ---------------------------------------------------------------------------


class TestYAMLConfigLoading:
    def test_load_sheets_config(self) -> None:
        config = load_connector_config(CONFIGS_DIR / "sheets.yaml")
        assert config.cli == "gws"
        assert config.service == "sheets"
        assert config.auth.provider == "google"
        assert "https://www.googleapis.com/auth/spreadsheets" in config.auth.scopes
        assert config.read is not None
        assert config.read.format == "json"
        assert len(config.write) == 2
        assert config.write[0].operation == "append_rows"
        assert config.write[1].operation == "update_cells"
        assert config.sync is not None
        assert config.sync.page_size == 50

    def test_load_docs_config(self) -> None:
        config = load_connector_config(CONFIGS_DIR / "docs.yaml")
        assert config.cli == "gws"
        assert config.service == "docs"
        assert config.auth.provider == "google"
        assert "https://www.googleapis.com/auth/documents" in config.auth.scopes
        assert config.read is not None
        assert len(config.write) == 2
        assert config.write[0].operation == "insert_text"
        assert config.write[1].operation == "replace_text"
        assert config.sync is not None
        assert config.sync.state_field == "lastModifiedTime"

    def test_load_chat_config(self) -> None:
        config = load_connector_config(CONFIGS_DIR / "chat.yaml")
        assert config.cli == "gws"
        assert config.service == "chat"
        assert config.auth.provider == "google"
        assert len(config.auth.scopes) == 2
        assert config.read is not None
        assert len(config.write) == 2
        assert config.write[0].operation == "send_message"
        assert config.write[0].traits["reversibility"] == "none"
        assert config.write[0].traits["confirm"] == "user"
        assert config.write[1].operation == "create_space"
        assert config.write[1].traits["reversibility"] == "full"
        assert config.sync is not None
        assert config.sync.state_field == "eventTime"

    def test_sheets_write_schema_refs(self) -> None:
        config = load_connector_config(CONFIGS_DIR / "sheets.yaml")
        assert (
            config.write[0].schema_ref == "nexus.backends.connectors.gws.schemas.AppendRowsSchema"
        )
        assert (
            config.write[1].schema_ref == "nexus.backends.connectors.gws.schemas.UpdateCellsSchema"
        )

    def test_docs_write_schema_refs(self) -> None:
        config = load_connector_config(CONFIGS_DIR / "docs.yaml")
        assert (
            config.write[0].schema_ref == "nexus.backends.connectors.gws.schemas.InsertTextSchema"
        )
        assert (
            config.write[1].schema_ref == "nexus.backends.connectors.gws.schemas.ReplaceTextSchema"
        )

    def test_chat_write_schema_refs(self) -> None:
        config = load_connector_config(CONFIGS_DIR / "chat.yaml")
        assert (
            config.write[0].schema_ref == "nexus.backends.connectors.gws.schemas.SendMessageSchema"
        )
        assert (
            config.write[1].schema_ref == "nexus.backends.connectors.gws.schemas.CreateSpaceSchema"
        )
