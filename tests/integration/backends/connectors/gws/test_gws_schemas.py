"""Tests for Google Workspace CLI connector schemas and YAML configs (Phase 3, Issue #3148).

Covers:
- Valid schema construction for all 6 schemas (Sheets, Docs, Chat)
- Missing required fields rejected via model_validate
- Too-short agent_intent rejected
- Default values applied correctly
- YAML configs load and validate via load_connector_config
"""

import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from nexus.backends.connectors.cli.loader import load_connector_config
from nexus.backends.connectors.cli.result import CLIResult, CLIResultStatus
from nexus.backends.connectors.gws.connector import (
    CalendarConnector,
    ChatConnector,
    DocsConnector,
    DriveConnector,
    GmailConnector,
    SheetsConnector,
)
from nexus.backends.connectors.gws.schemas import (
    AppendRowsSchema,
    CreateSpaceSchema,
    DeleteFileSchema,
    InsertTextSchema,
    ReplaceTextSchema,
    SendMessageSchema,
    UpdateCellsSchema,
    UpdateFileSchema,
    UploadFileSchema,
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


class TestDocsConnectorListing:
    def test_list_dir_uses_drive_metadata_and_filters_to_docs(self) -> None:
        connector = DocsConnector()
        payload = json.dumps(
            {
                "files": [
                    {
                        "id": "docAlpha",
                        "name": "Doc Alpha",
                        "mimeType": "application/vnd.google-apps.document",
                    },
                    {"name": "Spreadsheet", "mimeType": "application/vnd.google-apps.spreadsheet"},
                    {
                        "id": "docBeta",
                        "name": "Doc Beta",
                        "mimeType": "application/vnd.google-apps.document",
                    },
                ]
            }
        )
        cast(Any, connector)._execute_cli = MagicMock(
            return_value=CLIResult(
                status=CLIResultStatus.SUCCESS,
                exit_code=0,
                stdout=payload,
                command=["gws", "drive", "files", "list"],
            )
        )

        result = connector.list_dir("")

        assert result == ["Doc Alpha", "Doc Beta"]
        connector._execute_cli.assert_called_once()
        args = connector._execute_cli.call_args.args[0]
        assert args[:4] == ["gws", "drive", "files", "list"]
        assert args[4] == "--params"
        assert "application/vnd.google-apps.document" in args[5]

    def test_list_dir_raises_on_cli_failure(self) -> None:
        connector = DocsConnector()
        cast(Any, connector)._execute_cli = MagicMock(
            return_value=CLIResult(
                status=CLIResultStatus.EXIT_ERROR,
                exit_code=1,
                stderr="403 permission denied",
                command=["gws", "drive", "files", "list"],
            )
        )

        with pytest.raises(Exception, match="permission denied"):
            connector.list_dir("")

    def test_list_dir_disambiguates_duplicate_names(self) -> None:
        connector = DocsConnector()
        payload = json.dumps(
            {
                "files": [
                    {
                        "id": "docA",
                        "name": "Shared Name",
                        "mimeType": "application/vnd.google-apps.document",
                    },
                    {
                        "id": "docB",
                        "name": "Shared Name",
                        "mimeType": "application/vnd.google-apps.document",
                    },
                ]
            }
        )
        cast(Any, connector)._execute_cli = MagicMock(
            return_value=CLIResult(
                status=CLIResultStatus.SUCCESS,
                exit_code=0,
                stdout=payload,
                command=["gws", "drive", "files", "list"],
            )
        )

        result = connector.list_dir("")

        assert result == ["Shared Name [docA]", "Shared Name [docB]"]

    def test_read_content_uses_disambiguated_id_suffix(self) -> None:
        connector = DocsConnector()
        cast(Any, connector)._execute_cli = MagicMock(
            return_value=CLIResult(
                status=CLIResultStatus.SUCCESS,
                exit_code=0,
                stdout='{"documentId":"docA","title":"Shared Name"}',
                command=["gws", "docs", "documents", "get"],
            )
        )

        from nexus.contracts.types import OperationContext

        context = OperationContext(
            user_id="alice@example.com",
            groups=[],
            backend_path="Shared Name [docA]",
            virtual_path="/gws/docs/Shared Name [docA]",
        )
        result = connector.read_content("", context=context)

        assert b'"documentId":"docA"' in result
        args = cast(Any, connector)._execute_cli.call_args.args[0]
        assert args[:3] == ["gws", "docs", "documents"]
        assert args[3] == "get"
        assert args[4] == "--params"
        assert '"documentId": "docA"' in args[5]


class TestSheetsConnectorListing:
    def test_list_dir_uses_drive_metadata_and_filters_to_spreadsheets(self) -> None:
        connector = SheetsConnector()
        payload = json.dumps(
            {
                "files": [
                    {
                        "name": "Quarterly Plan",
                        "mimeType": "application/vnd.google-apps.spreadsheet",
                    },
                    {"name": "Doc Alpha", "mimeType": "application/vnd.google-apps.document"},
                    {"name": "Pipeline", "mimeType": "application/vnd.google-apps.spreadsheet"},
                ]
            }
        )
        cast(Any, connector)._execute_cli = MagicMock(
            return_value=CLIResult(
                status=CLIResultStatus.SUCCESS,
                exit_code=0,
                stdout=payload,
                command=["gws", "drive", "files", "list"],
            )
        )

        result = connector.list_dir("")

        assert result == ["Pipeline", "Quarterly Plan"]
        connector._execute_cli.assert_called_once()
        args = connector._execute_cli.call_args.args[0]
        assert args[:4] == ["gws", "drive", "files", "list"]
        assert args[4] == "--params"
        assert "application/vnd.google-apps.spreadsheet" in args[5]


class TestDriveConnectorListing:
    def test_list_dir_marks_folders_and_files(self) -> None:
        connector = DriveConnector()
        payload = json.dumps(
            {
                "files": [
                    {"name": "Specs", "mimeType": "application/vnd.google-apps.folder"},
                    {"name": "notes.txt", "mimeType": "text/plain"},
                    {"name": "Roadmap", "mimeType": "application/vnd.google-apps.document"},
                ]
            }
        )
        cast(Any, connector)._execute_cli = MagicMock(
            return_value=CLIResult(
                status=CLIResultStatus.SUCCESS,
                exit_code=0,
                stdout=payload,
                command=["gws", "drive", "files", "list"],
            )
        )

        result = connector.list_dir("")

        assert result == ["Roadmap", "Specs/", "notes.txt"]


class TestChatConnectorListing:
    def test_list_dir_root_lists_spaces(self) -> None:
        connector = ChatConnector()
        payload = json.dumps(
            {
                "spaces": [
                    {"name": "spaces/AAA"},
                    {"name": "spaces/BBB"},
                ]
            }
        )
        cast(Any, connector)._execute_cli = MagicMock(
            return_value=CLIResult(
                status=CLIResultStatus.SUCCESS,
                exit_code=0,
                stdout=payload,
                command=["gws", "chat", "spaces", "list"],
            )
        )

        result = connector.list_dir("")

        assert result == ["spaces/AAA/", "spaces/BBB/"]

    def test_list_dir_raises_actionable_error_on_missing_chat_scopes(self) -> None:
        connector = ChatConnector()
        cast(Any, connector)._execute_cli = MagicMock(
            return_value=CLIResult(
                status=CLIResultStatus.EXIT_ERROR,
                exit_code=1,
                stdout='{"error":{"message":"Request had insufficient authentication scopes."}}',
                stderr="error[api]: Request had insufficient authentication scopes.",
                command=["gws", "chat", "spaces", "list"],
            )
        )

        with pytest.raises(Exception, match="approve Chat access"):
            connector.list_dir("")


class TestGmailConnectorListing:
    def test_list_dir_root_returns_expected_label_folders(self) -> None:
        connector = GmailConnector()
        assert connector.list_dir("") == [
            "INBOX/",
            "SENT/",
            "STARRED/",
            "IMPORTANT/",
            "DRAFTS/",
        ]


class TestCalendarConnectorListing:
    def test_list_dir_root_lists_calendars(self) -> None:
        connector = CalendarConnector()
        cast(Any, connector)._execute_cli = MagicMock(
            return_value=CLIResult(
                status=CLIResultStatus.SUCCESS,
                exit_code=0,
                stdout='items:\n  - id: "primary"\n  - id: "team@example.com"\n',
                command=["gws", "calendar", "calendarList", "list", "--format", "yaml"],
            )
        )

        result = connector.list_dir("")

        assert result == ["primary/", "team@example.com/"]


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

    # --- Gmail YAML config ---

    def test_load_gmail_config(self) -> None:
        config = load_connector_config(CONFIGS_DIR / "gmail.yaml")
        assert config.cli == "gws"
        assert config.service == "gmail"
        assert config.auth.provider == "google"
        assert len(config.auth.scopes) == 3
        assert "https://www.googleapis.com/auth/gmail.send" in config.auth.scopes
        assert config.read is not None
        assert config.read.format == "yaml"
        assert len(config.write) == 4
        assert config.write[0].operation == "send_email"
        assert config.write[0].traits["reversibility"] == "none"
        assert config.write[0].traits["confirm"] == "user"
        assert config.write[3].operation == "create_draft"
        assert config.write[3].traits["reversibility"] == "full"
        assert config.write[3].traits["confirm"] == "intent"
        assert config.sync is not None
        assert config.sync.state_field == "historyId"
        assert config.sync.page_size == 100

    def test_gmail_write_schema_refs(self) -> None:
        config = load_connector_config(CONFIGS_DIR / "gmail.yaml")
        assert (
            config.write[0].schema_ref == "nexus.backends.connectors.gmail.schemas.SendEmailSchema"
        )
        assert (
            config.write[1].schema_ref == "nexus.backends.connectors.gmail.schemas.ReplyEmailSchema"
        )
        assert (
            config.write[2].schema_ref
            == "nexus.backends.connectors.gmail.schemas.ForwardEmailSchema"
        )
        assert (
            config.write[3].schema_ref == "nexus.backends.connectors.gmail.schemas.DraftEmailSchema"
        )

    # --- Calendar YAML config ---

    def test_load_calendar_config(self) -> None:
        config = load_connector_config(CONFIGS_DIR / "calendar.yaml")
        assert config.cli == "gws"
        assert config.service == "calendar"
        assert config.auth.provider == "google"
        assert "https://www.googleapis.com/auth/calendar" in config.auth.scopes
        assert config.read is not None
        assert config.read.format == "json"
        assert len(config.write) == 3
        assert config.write[0].operation == "create_event"
        assert config.write[0].traits["reversibility"] == "full"
        assert config.write[0].traits["confirm"] == "intent"
        assert config.write[1].operation == "update_event"
        assert config.write[2].operation == "delete_event"
        assert config.write[2].traits["reversibility"] == "partial"
        assert config.write[2].traits["confirm"] == "user"
        assert config.sync is not None
        assert config.sync.state_field == "syncToken"
        assert config.sync.page_size == 100

    def test_calendar_write_schema_refs(self) -> None:
        config = load_connector_config(CONFIGS_DIR / "calendar.yaml")
        assert (
            config.write[0].schema_ref
            == "nexus.backends.connectors.calendar.schemas.CreateEventSchema"
        )
        assert (
            config.write[1].schema_ref
            == "nexus.backends.connectors.calendar.schemas.UpdateEventSchema"
        )
        assert (
            config.write[2].schema_ref
            == "nexus.backends.connectors.calendar.schemas.DeleteEventSchema"
        )

    # --- Drive YAML config ---

    def test_load_drive_config(self) -> None:
        config = load_connector_config(CONFIGS_DIR / "drive.yaml")
        assert config.cli == "gws"
        assert config.service == "drive"
        assert config.auth.provider == "google"
        assert "https://www.googleapis.com/auth/drive" in config.auth.scopes
        assert config.read is not None
        assert config.read.format == "json"
        assert len(config.write) == 3
        assert config.write[0].operation == "upload_file"
        assert config.write[0].traits["reversibility"] == "full"
        assert config.write[0].traits["confirm"] == "intent"
        assert config.write[1].operation == "update_file"
        assert config.write[1].traits["reversibility"] == "partial"
        assert config.write[1].traits["confirm"] == "explicit"
        assert config.write[2].operation == "delete_file"
        assert config.write[2].traits["reversibility"] == "partial"
        assert config.write[2].traits["confirm"] == "user"
        assert config.sync is not None
        assert config.sync.state_field == "newStartPageToken"
        assert config.sync.page_size == 100

    def test_drive_write_schema_refs(self) -> None:
        config = load_connector_config(CONFIGS_DIR / "drive.yaml")
        assert (
            config.write[0].schema_ref == "nexus.backends.connectors.gws.schemas.UploadFileSchema"
        )
        assert (
            config.write[1].schema_ref == "nexus.backends.connectors.gws.schemas.UpdateFileSchema"
        )
        assert (
            config.write[2].schema_ref == "nexus.backends.connectors.gws.schemas.DeleteFileSchema"
        )


# ---------------------------------------------------------------------------
# Google Drive — UploadFileSchema
# ---------------------------------------------------------------------------


class TestUploadFileSchema:
    def test_valid_upload(self) -> None:
        schema = UploadFileSchema(
            agent_intent="Uploading quarterly revenue report for team review",
            name="Q1-report.pdf",
        )
        assert schema.name == "Q1-report.pdf"
        assert schema.parent_id is None
        assert schema.mime_type is None
        assert schema.content_path is None
        assert schema.description == ""
        assert schema.confirm is False

    def test_full_upload(self) -> None:
        schema = UploadFileSchema(
            agent_intent="Uploading design mockup to shared project folder",
            name="mockup-v2.png",
            parent_id="1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
            mime_type="image/png",
            content_path="/tmp/mockup-v2.png",
            description="Updated mockup with feedback incorporated",
            confirm=True,
        )
        assert schema.parent_id is not None
        assert schema.mime_type == "image/png"
        assert schema.confirm is True

    def test_missing_name(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            UploadFileSchema.model_validate({"agent_intent": "Uploading a file for the project"})
        assert "name" in str(exc_info.value)

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            UploadFileSchema(
                agent_intent="Uploading a file for the project",
                name="",
            )

    def test_name_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            UploadFileSchema(
                agent_intent="Uploading a file for the project",
                name="x" * 1025,
            )

    def test_short_intent_rejected(self) -> None:
        with pytest.raises(ValidationError):
            UploadFileSchema(
                agent_intent="short",
                name="file.txt",
            )


# ---------------------------------------------------------------------------
# Google Drive — UpdateFileSchema
# ---------------------------------------------------------------------------


class TestUpdateFileSchema:
    def test_valid_update(self) -> None:
        schema = UpdateFileSchema(
            agent_intent="Renaming file to match new naming convention",
            file_id="1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
            name="new-name.txt",
        )
        assert schema.file_id.startswith("1Bxi")
        assert schema.name == "new-name.txt"
        assert schema.description is None
        assert schema.parent_id is None
        assert schema.starred is None
        assert schema.confirm is False

    def test_missing_file_id(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            UpdateFileSchema.model_validate(
                {"agent_intent": "Updating file metadata for organization"}
            )
        assert "file_id" in str(exc_info.value)

    def test_star_file(self) -> None:
        schema = UpdateFileSchema(
            agent_intent="Starring important project document for quick access",
            file_id="abc123",
            starred=True,
        )
        assert schema.starred is True

    def test_short_intent_rejected(self) -> None:
        with pytest.raises(ValidationError):
            UpdateFileSchema(
                agent_intent="short",
                file_id="abc123",
            )


# ---------------------------------------------------------------------------
# Google Drive — DeleteFileSchema
# ---------------------------------------------------------------------------


class TestDeleteFileSchema:
    def test_valid_delete(self) -> None:
        schema = DeleteFileSchema(
            agent_intent="Deleting outdated draft that is no longer needed",
            file_id="1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
        )
        assert schema.file_id.startswith("1Bxi")
        assert schema.user_confirmed is False

    def test_confirmed_delete(self) -> None:
        schema = DeleteFileSchema(
            agent_intent="Deleting duplicate file after confirming with user",
            file_id="abc123",
            user_confirmed=True,
        )
        assert schema.user_confirmed is True

    def test_missing_file_id(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            DeleteFileSchema.model_validate(
                {"agent_intent": "Deleting a file that is no longer needed"}
            )
        assert "file_id" in str(exc_info.value)

    def test_short_intent_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DeleteFileSchema(
                agent_intent="short",
                file_id="abc123",
            )


# ---------------------------------------------------------------------------
# Display path tests (Issue #3256)
# ---------------------------------------------------------------------------


class TestGmailDisplayPath:
    """Test GmailConnector.display_path() for human-readable email paths."""

    def _connector(self):
        from nexus.backends.connectors.gws.connector import GmailConnector

        return GmailConnector.__new__(GmailConnector)

    def test_full_metadata(self) -> None:
        c = self._connector()
        path = c.display_path(
            "msg-123",
            {
                "subject": "Re: Meeting Notes",
                "date": "2026-03-20T10:30:00Z",
                "labels": ["INBOX", "CATEGORY_PERSONAL"],
            },
        )
        assert path.startswith("INBOX/PRIMARY/")
        assert "2026-03-20" in path
        assert "Re-Meeting-Notes" in path
        assert path.endswith(".yaml")

    def test_social_category(self) -> None:
        c = self._connector()
        path = c.display_path(
            "msg-456",
            {
                "subject": "New follower",
                "date": "2026-03-20",
                "labels": ["INBOX", "CATEGORY_SOCIAL"],
            },
        )
        assert "INBOX/SOCIAL/" in path

    def test_no_subject_falls_back_to_id(self) -> None:
        c = self._connector()
        path = c.display_path("msg-789", {"labels": ["INBOX"]})
        assert "msg-789" in path
        assert path.endswith(".yaml")

    def test_empty_metadata(self) -> None:
        c = self._connector()
        path = c.display_path("msg-000", {})
        assert "INBOX/PRIMARY/" in path
        assert "msg-000" in path

    def test_sent_label(self) -> None:
        c = self._connector()
        path = c.display_path(
            "msg-sent",
            {
                "subject": "Hello",
                "labels": ["SENT"],
            },
        )
        assert path.startswith("SENT/")
        assert "PRIMARY" not in path  # No categories for SENT

    def test_no_metadata(self) -> None:
        c = self._connector()
        path = c.display_path("msg-none", None)
        assert path.endswith(".yaml")

    def test_unparseable_date_no_leading_underscore(self) -> None:
        """Unparseable date should not produce a leading underscore in the filename."""
        c = self._connector()
        path = c.display_path(
            "msg-bad-date",
            {
                "subject": "Hello",
                "date": "not-a-date",
                "labels": ["INBOX"],
            },
        )
        filename = path.rsplit("/", 1)[-1]
        assert not filename.startswith("_"), f"Leading underscore in filename: {path}"
        assert "Hello" in path

    def test_internal_date_timestamp(self) -> None:
        """Gmail internalDate (ms timestamp) should not crash."""
        c = self._connector()
        path = c.display_path(
            "msg-ts",
            {
                "subject": "Test",
                "internalDate": "1711027200000",
                "labels": ["INBOX"],
            },
        )
        assert path.endswith(".yaml")
        assert "Test" in path


class TestCalendarDisplayPath:
    """Test CalendarConnector.display_path() for human-readable event paths."""

    def _connector(self):
        from nexus.backends.connectors.gws.connector import CalendarConnector

        return CalendarConnector.__new__(CalendarConnector)

    def test_full_event(self) -> None:
        c = self._connector()
        path = c.display_path(
            "evt-123",
            {
                "summary": "Team Standup",
                "start": {"dateTime": "2026-03-21T10:00:00-07:00"},
                "calendarId": "primary",
            },
        )
        assert "primary/" in path
        assert "2026-03/" in path
        assert "2026-03-21" in path
        assert "10-00" in path
        assert "Team-Standup" in path
        assert path.endswith(".yaml")

    def test_all_day_event(self) -> None:
        c = self._connector()
        path = c.display_path(
            "evt-456",
            {
                "summary": "Company Offsite",
                "start": {"date": "2026-04-01"},
                "calendarId": "primary",
            },
        )
        assert "2026-04-01" in path
        assert "Company-Offsite" in path

    def test_no_summary(self) -> None:
        c = self._connector()
        path = c.display_path(
            "evt-789",
            {
                "start": {"dateTime": "2026-03-21T10:00:00Z"},
            },
        )
        assert "evt-789" in path

    def test_no_metadata(self) -> None:
        c = self._connector()
        path = c.display_path("evt-000", None)
        assert path == "primary/evt-000.yaml"


class TestDriveDisplayPath:
    """Test DriveConnector.display_path() for Drive files."""

    def _connector(self):
        from nexus.backends.connectors.gws.connector import DriveConnector

        return DriveConnector.__new__(DriveConnector)

    def test_preserves_original_filename(self) -> None:
        c = self._connector()
        path = c.display_path("file-abc", {"name": "Q4 Report.pdf"})
        assert "Q4-Report.pdf" in path

    def test_uses_title_field(self) -> None:
        c = self._connector()
        path = c.display_path("file-abc", {"title": "Design Doc"})
        assert "Design-Doc" in path

    def test_no_metadata_falls_back(self) -> None:
        c = self._connector()
        path = c.display_path("file-abc", None)
        assert path == "file-abc.yaml"


# ---------------------------------------------------------------------------
# Issue #3713 — CLIResult.as_yaml() / as_json() preamble stripping
# ---------------------------------------------------------------------------


class TestCLIResultParsing:
    """as_yaml() and as_json() strip CLI preamble and raise ValueError on bad output."""

    def _make(self, stdout: str) -> CLIResult:
        return CLIResult(status=CLIResultStatus.SUCCESS, stdout=stdout)

    # --- as_yaml ---

    def test_as_yaml_no_preamble(self) -> None:
        r = self._make("id: abc\nfoo: bar\n")
        assert r.as_yaml() == {"id": "abc", "foo": "bar"}

    def test_as_yaml_strips_keyring_preamble(self) -> None:
        r = self._make("Using keyring backend for credentials\nid: abc\nfoo: bar\n")
        assert r.as_yaml() == {"id": "abc", "foo": "bar"}

    def test_as_yaml_strips_multi_line_preamble(self) -> None:
        r = self._make("Line one\nLine two\nkey: value\n")
        assert r.as_yaml() == {"key": "value"}

    def test_as_yaml_raises_on_garbage(self) -> None:
        r = self._make("!!not valid yaml [\x00")
        with pytest.raises(ValueError, match="Failed to parse CLI output as YAML"):
            r.as_yaml()

    # --- as_json ---

    def test_as_json_no_preamble(self) -> None:
        r = self._make('{"key": "value"}')
        assert r.as_json() == {"key": "value"}

    def test_as_json_strips_preamble(self) -> None:
        r = self._make('Some preamble text\n{"key": "value"}')
        assert r.as_json() == {"key": "value"}

    def test_as_json_raises_on_garbage(self) -> None:
        r = self._make("not json at all")
        with pytest.raises(ValueError, match="Failed to parse CLI output as JSON"):
            r.as_json()


# ---------------------------------------------------------------------------
# Issue #3713 — _gmail_utils.extract_body: MIME shape coverage
# ---------------------------------------------------------------------------


class TestExtractBody:
    """extract_body() handles all MIME shapes correctly."""

    import base64 as _base64

    from nexus.backends.connectors.gws._gmail_utils import extract_body as _extract_body

    @staticmethod
    def _b64(text: str) -> str:
        import base64

        return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")

    def test_single_part_plain(self) -> None:
        from nexus.backends.connectors.gws._gmail_utils import extract_body

        payload = {"mimeType": "text/plain", "body": {"data": self._b64("Hello World")}}
        assert extract_body(payload) == "Hello World"

    def test_multipart_alternative_prefers_plain(self) -> None:
        from nexus.backends.connectors.gws._gmail_utils import extract_body

        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": self._b64("Plain text")}},
                {"mimeType": "text/html", "body": {"data": self._b64("<b>HTML</b>")}},
            ],
        }
        assert extract_body(payload) == "Plain text"

    def test_html_only_fallback(self) -> None:
        from nexus.backends.connectors.gws._gmail_utils import extract_body

        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/html", "body": {"data": self._b64("<b>HTML only</b>")}},
            ],
        }
        assert extract_body(payload) == "<b>HTML only</b>"

    def test_multipart_mixed_extracts_plain_from_nested_part(self) -> None:
        from nexus.backends.connectors.gws._gmail_utils import extract_body

        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": self._b64("Nested plain")}},
                        {
                            "mimeType": "text/html",
                            "body": {"data": self._b64("<b>Nested HTML</b>")},
                        },
                    ],
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "attachment.pdf",
                    "body": {"data": self._b64("binary")},
                },
            ],
        }
        assert extract_body(payload) == "Nested plain"

    def test_empty_payload_returns_empty_string(self) -> None:
        from nexus.backends.connectors.gws._gmail_utils import extract_body

        assert extract_body({}) == ""
        assert extract_body({"mimeType": "multipart/mixed", "parts": []}) == ""


# ---------------------------------------------------------------------------
# Issue #3713 — GmailConnector.list_dir pagination and failure paths
# ---------------------------------------------------------------------------


def _gmail_list_result(messages: list[dict], next_token: str | None = None) -> CLIResult:
    """Build a CLIResult whose stdout is a gws messages.list YAML response."""
    import yaml as _yaml

    data: dict = {"messages": messages}
    if next_token:
        data["nextPageToken"] = next_token
    return CLIResult(
        status=CLIResultStatus.SUCCESS,
        stdout=_yaml.dump(data, default_flow_style=False),
        command=["gws", "gmail", "users", "messages", "list"],
    )


class TestGmailConnectorPagination:
    """list_dir paginates via nextPageToken and stops at MAX_LIST_RESULTS."""

    def _connector(self) -> GmailConnector:
        c = GmailConnector.__new__(GmailConnector)
        c._id_list_cache = {}
        c._backend_name = "cli:gws:gmail"
        return c

    def test_single_page_no_token(self) -> None:
        c = self._connector()
        cast(Any, c)._execute_cli = MagicMock(
            return_value=_gmail_list_result(
                [
                    {"id": "msg1", "threadId": "thr1"},
                    {"id": "msg2", "threadId": "thr2"},
                ]
            )
        )
        result = c.list_dir("INBOX/PRIMARY")
        assert result == ["thr1-msg1.yaml", "thr2-msg2.yaml"]
        assert cast(Any, c)._execute_cli.call_count == 1

    def test_two_page_pagination(self) -> None:
        c = self._connector()
        page1 = _gmail_list_result([{"id": "msg1", "threadId": "thr1"}], next_token="tok123")
        page2 = _gmail_list_result([{"id": "msg2", "threadId": "thr2"}])
        cast(Any, c)._execute_cli = MagicMock(side_effect=[page1, page2])

        result = c.list_dir("INBOX/PRIMARY")

        assert "thr1-msg1.yaml" in result
        assert "thr2-msg2.yaml" in result
        assert cast(Any, c)._execute_cli.call_count == 2
        # Second call must carry the pageToken from the first response.
        # Command layout: ["gws", "gmail", "users", "messages", "list",
        #                   "--params", <json>, "--format", "yaml"]
        #                   0       1       2         3       4     5       6         7      8
        second_call_params = json.loads(cast(Any, c)._execute_cli.call_args_list[1].args[0][6])
        assert second_call_params["pageToken"] == "tok123"

    def test_list_dir_returns_empty_on_cli_failure(self) -> None:
        c = self._connector()
        cast(Any, c)._execute_cli = MagicMock(
            return_value=CLIResult(
                status=CLIResultStatus.EXIT_ERROR,
                exit_code=1,
                stdout="",
                stderr="network error",
                command=["gws", "gmail", "users", "messages", "list"],
            )
        )
        result = c.list_dir("INBOX/PRIMARY")
        assert result == []

    def test_id_list_cache_avoids_second_call(self) -> None:
        c = self._connector()
        cast(Any, c)._execute_cli = MagicMock(
            return_value=_gmail_list_result([{"id": "msg1", "threadId": "thr1"}])
        )
        c.list_dir("INBOX/PRIMARY")
        c.list_dir("INBOX/PRIMARY")  # should hit cache
        assert cast(Any, c)._execute_cli.call_count == 1


# ---------------------------------------------------------------------------
# Issue #3713 — GmailConnector.read_content body extraction and error handling
# ---------------------------------------------------------------------------


def _gmail_get_result(msg: dict) -> CLIResult:
    """Build a CLIResult whose stdout is a gws messages.get YAML response."""
    import yaml as _yaml

    return CLIResult(
        status=CLIResultStatus.SUCCESS,
        stdout=_yaml.dump(msg, default_flow_style=False),
        command=["gws", "gmail", "users", "messages", "get"],
    )


class TestGmailConnectorReadContent:
    """read_content extracts body + headers and raises on CLI failure."""

    import base64 as _base64

    @staticmethod
    def _b64(text: str) -> str:
        import base64

        return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")

    def _connector(self) -> GmailConnector:
        c = GmailConnector.__new__(GmailConnector)
        c._id_list_cache = {}
        c._backend_name = "cli:gws:gmail"
        return c

    def _context(self, path: str) -> Any:
        from nexus.contracts.types import OperationContext

        return OperationContext(
            user_id="alice@example.com",
            groups=[],
            backend_path=path,
            virtual_path=f"/gws/gmail/{path}",
        )

    def test_extracts_plain_body_and_headers(self) -> None:
        c = self._connector()
        import yaml as _yaml

        msg = {
            "id": "msg001",
            "threadId": "thr001",
            "labelIds": ["INBOX", "CATEGORY_PERSONAL"],
            "snippet": "Hello there",
            "historyId": "999",
            "internalDate": "1710720000000",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Test Subject"},
                    {"name": "From", "value": "sender@example.com"},
                    {"name": "To", "value": "alice@example.com"},
                    {"name": "Date", "value": "Wed, 20 Mar 2026 10:00:00 +0000"},
                ],
                "mimeType": "text/plain",
                "body": {"data": self._b64("Hello, this is the body.")},
            },
        }
        cast(Any, c)._execute_cli = MagicMock(return_value=_gmail_get_result(msg))
        ctx = self._context("INBOX/PRIMARY/thr001-msg001.yaml")

        raw = c.read_content("msg001", context=ctx)
        result = _yaml.safe_load(raw)

        assert result["body"] == "Hello, this is the body."
        assert result["subject"] == "Test Subject"
        assert result["from"] == "sender@example.com"
        assert result["id"] == "msg001"
        assert result["threadId"] == "thr001"
        assert "INBOX" in result["labels"]

    def test_read_content_raises_on_cli_failure(self) -> None:
        from nexus.contracts.exceptions import BackendError

        c = self._connector()
        cast(Any, c)._execute_cli = MagicMock(
            return_value=CLIResult(
                status=CLIResultStatus.EXIT_ERROR,
                exit_code=1,
                stdout="",
                stderr="401 Unauthorized",
                command=["gws", "gmail", "users", "messages", "get"],
            )
        )
        with pytest.raises(BackendError):
            c.read_content("badmsg")

    def test_msg_id_extracted_from_context_path(self) -> None:
        c = self._connector()
        called_params: list[dict] = []

        def _fake_cli(cmd: list[str], **_: Any) -> CLIResult:
            # Command: ["gws", "gmail", "users", "messages", "get",
            #           "--params", <json>, "--format", "yaml"]
            params = json.loads(cmd[6])
            called_params.append(params)
            return _gmail_get_result(
                {
                    "id": params["id"],
                    "payload": {
                        "mimeType": "text/plain",
                        "headers": [],
                        "body": {"data": self._b64("body")},
                    },
                }
            )

        cast(Any, c)._execute_cli = _fake_cli
        ctx = self._context("INBOX/PRIMARY/thread001-targetmsg.yaml")
        c.read_content("ignored_hash", context=ctx)
        assert called_params[0]["id"] == "targetmsg"

    def test_preamble_in_get_response_is_stripped(self) -> None:
        import yaml as _yaml

        c = self._connector()
        msg = {
            "id": "msg002",
            "payload": {
                "mimeType": "text/plain",
                "headers": [{"name": "Subject", "value": "Hi"}],
                "body": {"data": self._b64("Body text")},
            },
        }
        preamble_stdout = "Using keyring backend\n" + _yaml.dump(msg)
        cast(Any, c)._execute_cli = MagicMock(
            return_value=CLIResult(
                status=CLIResultStatus.SUCCESS,
                stdout=preamble_stdout,
                command=["gws", "gmail", "users", "messages", "get"],
            )
        )
        raw = c.read_content("msg002")
        result = _yaml.safe_load(raw)
        assert result["body"] == "Body text"
        assert result["subject"] == "Hi"
