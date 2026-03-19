"""Concrete Google Workspace CLI connector classes.

Each class is a CLIConnector subclass with baked-in schemas, traits, and
CLI configuration. Instantiate directly or via ``create_connector_from_yaml()``
with the corresponding YAML config.

Phase 3 (Issue #3148).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from nexus.backends.connectors.base import (
    ConfirmLevel,
    ErrorDef,
    OpTraits,
    Reversibility,
)
from nexus.backends.connectors.calendar.schemas import (
    CreateEventSchema,
    DeleteEventSchema,
    UpdateEventSchema,
)
from nexus.backends.connectors.cli.base import CLIConnector
from nexus.backends.connectors.cli.config import CLIConnectorConfig

# Gmail/Calendar schemas live in their own packages (existing API connectors)
from nexus.backends.connectors.gmail.schemas import (
    DraftEmailSchema,
    ForwardEmailSchema,
    ReplyEmailSchema,
    SendEmailSchema,
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

logger = logging.getLogger(__name__)

_CONFIGS_DIR = Path(__file__).parent / "configs"


class SheetsConnector(CLIConnector):
    """Google Sheets CLI connector via ``gws sheets``."""

    SKILL_NAME = "sheets"
    CLI_NAME = "gws"
    CLI_SERVICE = "sheets"

    SCHEMAS: dict[str, type] = {
        "append_rows": AppendRowsSchema,
        "update_cells": UpdateCellsSchema,
    }
    OPERATION_TRAITS: dict[str, OpTraits] = {
        "append_rows": OpTraits(reversibility=Reversibility.PARTIAL, confirm=ConfirmLevel.INTENT),
        "update_cells": OpTraits(
            reversibility=Reversibility.PARTIAL, confirm=ConfirmLevel.EXPLICIT
        ),
    }
    ERROR_REGISTRY: dict[str, ErrorDef] = {
        "MISSING_AGENT_INTENT": ErrorDef(
            message="Operations require agent_intent",
            skill_section="required-format",
        ),
        "SPREADSHEET_NOT_FOUND": ErrorDef(
            message="Spreadsheet not found",
            skill_section="operations",
            fix_example="spreadsheet_id: <valid spreadsheet ID or URL>",
        ),
    }

    def __init__(self, **kwargs: Any) -> None:
        config = self._load_config("sheets.yaml")
        kwargs.setdefault("config", config)
        super().__init__(**kwargs)

    @staticmethod
    def _load_config(filename: str) -> CLIConnectorConfig | None:
        config_path = _CONFIGS_DIR / filename
        if config_path.exists():
            from nexus.backends.connectors.cli.loader import load_connector_config

            return load_connector_config(config_path)
        return None


class DocsConnector(CLIConnector):
    """Google Docs CLI connector via ``gws docs``."""

    SKILL_NAME = "docs"
    CLI_NAME = "gws"
    CLI_SERVICE = "docs"

    SCHEMAS: dict[str, type] = {
        "insert_text": InsertTextSchema,
        "replace_text": ReplaceTextSchema,
    }
    OPERATION_TRAITS: dict[str, OpTraits] = {
        "insert_text": OpTraits(reversibility=Reversibility.PARTIAL, confirm=ConfirmLevel.INTENT),
        "replace_text": OpTraits(
            reversibility=Reversibility.PARTIAL, confirm=ConfirmLevel.EXPLICIT
        ),
    }
    ERROR_REGISTRY: dict[str, ErrorDef] = {
        "MISSING_AGENT_INTENT": ErrorDef(
            message="Operations require agent_intent",
            skill_section="required-format",
        ),
        "DOCUMENT_NOT_FOUND": ErrorDef(
            message="Document not found",
            skill_section="operations",
            fix_example="document_id: <valid document ID>",
        ),
    }

    def __init__(self, **kwargs: Any) -> None:
        config = SheetsConnector._load_config("docs.yaml")
        kwargs.setdefault("config", config)
        super().__init__(**kwargs)


class ChatConnector(CLIConnector):
    """Google Chat CLI connector via ``gws chat``."""

    SKILL_NAME = "chat"
    CLI_NAME = "gws"
    CLI_SERVICE = "chat"

    SCHEMAS: dict[str, type] = {
        "send_message": SendMessageSchema,
        "create_space": CreateSpaceSchema,
    }
    OPERATION_TRAITS: dict[str, OpTraits] = {
        "send_message": OpTraits(reversibility=Reversibility.NONE, confirm=ConfirmLevel.USER),
        "create_space": OpTraits(reversibility=Reversibility.FULL, confirm=ConfirmLevel.EXPLICIT),
    }
    ERROR_REGISTRY: dict[str, ErrorDef] = {
        "MISSING_AGENT_INTENT": ErrorDef(
            message="Operations require agent_intent",
            skill_section="required-format",
        ),
        "SPACE_NOT_FOUND": ErrorDef(
            message="Chat space not found",
            skill_section="operations",
            fix_example="space: <valid space name or ID>",
        ),
    }

    def __init__(self, **kwargs: Any) -> None:
        config = SheetsConnector._load_config("chat.yaml")
        kwargs.setdefault("config", config)
        super().__init__(**kwargs)


class DriveConnector(CLIConnector):
    """Google Drive CLI connector via ``gws drive``."""

    SKILL_NAME = "drive"
    CLI_NAME = "gws"
    CLI_SERVICE = "drive"

    SCHEMAS: dict[str, type] = {
        "upload_file": UploadFileSchema,
        "update_file": UpdateFileSchema,
        "delete_file": DeleteFileSchema,
    }
    OPERATION_TRAITS: dict[str, OpTraits] = {
        "upload_file": OpTraits(reversibility=Reversibility.FULL, confirm=ConfirmLevel.INTENT),
        "update_file": OpTraits(reversibility=Reversibility.PARTIAL, confirm=ConfirmLevel.EXPLICIT),
        "delete_file": OpTraits(reversibility=Reversibility.PARTIAL, confirm=ConfirmLevel.USER),
    }
    ERROR_REGISTRY: dict[str, ErrorDef] = {
        "MISSING_AGENT_INTENT": ErrorDef(
            message="Operations require agent_intent",
            skill_section="required-format",
        ),
        "FILE_NOT_FOUND": ErrorDef(
            message="File not found in Drive",
            skill_section="operations",
            fix_example="file_id: <valid Drive file ID>",
        ),
    }

    def __init__(self, **kwargs: Any) -> None:
        config = SheetsConnector._load_config("drive.yaml")
        kwargs.setdefault("config", config)
        super().__init__(**kwargs)


class GmailConnector(CLIConnector):
    """Gmail CLI connector via ``gws gmail``.

    CLI-backed alternative to the existing GmailConnectorBackend API connector.
    Uses gws CLI for all operations. Phase 3 (Issue #3148).
    """

    SKILL_NAME = "gmail"
    CLI_NAME = "gws"
    CLI_SERVICE = "gmail"

    SCHEMAS: dict[str, type] = {
        "send_email": SendEmailSchema,
        "reply_email": ReplyEmailSchema,
        "forward_email": ForwardEmailSchema,
        "create_draft": DraftEmailSchema,
    }
    OPERATION_TRAITS: dict[str, OpTraits] = {
        "send_email": OpTraits(reversibility=Reversibility.NONE, confirm=ConfirmLevel.USER),
        "reply_email": OpTraits(reversibility=Reversibility.NONE, confirm=ConfirmLevel.USER),
        "forward_email": OpTraits(reversibility=Reversibility.NONE, confirm=ConfirmLevel.USER),
        "create_draft": OpTraits(reversibility=Reversibility.FULL, confirm=ConfirmLevel.INTENT),
    }
    ERROR_REGISTRY: dict[str, ErrorDef] = {
        "MISSING_AGENT_INTENT": ErrorDef(
            message="Operations require agent_intent",
            skill_section="required-format",
        ),
        "MISSING_RECIPIENTS": ErrorDef(
            message="Email requires at least one recipient",
            skill_section="operations",
            fix_example="to:\n  - user@example.com",
        ),
    }

    def __init__(self, **kwargs: Any) -> None:
        config = SheetsConnector._load_config("gmail.yaml")
        kwargs.setdefault("config", config)
        super().__init__(**kwargs)


class CalendarConnector(CLIConnector):
    """Calendar CLI connector via ``gws calendar``.

    CLI-backed alternative to the existing GoogleCalendarConnectorBackend.
    Uses gws CLI for all operations. Phase 3 (Issue #3148).
    """

    SKILL_NAME = "gcalendar"
    CLI_NAME = "gws"
    CLI_SERVICE = "calendar"

    SCHEMAS: dict[str, type] = {
        "create_event": CreateEventSchema,
        "update_event": UpdateEventSchema,
        "delete_event": DeleteEventSchema,
    }
    OPERATION_TRAITS: dict[str, OpTraits] = {
        "create_event": OpTraits(reversibility=Reversibility.FULL, confirm=ConfirmLevel.INTENT),
        "update_event": OpTraits(reversibility=Reversibility.FULL, confirm=ConfirmLevel.EXPLICIT),
        "delete_event": OpTraits(reversibility=Reversibility.PARTIAL, confirm=ConfirmLevel.USER),
    }
    ERROR_REGISTRY: dict[str, ErrorDef] = {
        "MISSING_AGENT_INTENT": ErrorDef(
            message="Operations require agent_intent",
            skill_section="required-format",
        ),
        "EVENT_NOT_FOUND": ErrorDef(
            message="Calendar event not found",
            skill_section="operations",
            fix_example="event_id: <valid event ID>",
        ),
    }

    def __init__(self, **kwargs: Any) -> None:
        config = SheetsConnector._load_config("calendar.yaml")
        kwargs.setdefault("config", config)
        super().__init__(**kwargs)
