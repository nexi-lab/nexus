"""Google Workspace CLI connectors — Sheets, Docs, Chat.

Phase 3 connectors (Issue #3148, Decision #3). Gmail, Calendar, and Drive
remain as existing API connectors; this package adds CLI-backed connectors
for services that benefit from the declarative YAML + Pydantic config model.
"""

from nexus.backends.connectors.gws.schemas import (
    AppendRowsSchema,
    CreateSpaceSchema,
    InsertTextSchema,
    ReplaceTextSchema,
    SendMessageSchema,
    UpdateCellsSchema,
)

__all__ = [
    # Sheets
    "AppendRowsSchema",
    "UpdateCellsSchema",
    # Docs
    "InsertTextSchema",
    "ReplaceTextSchema",
    # Chat
    "SendMessageSchema",
    "CreateSpaceSchema",
]
