"""Google Workspace CLI connectors — Sheets, Docs, Chat, Drive.

Phase 3 connectors (Issue #3148, Decision #3). Gmail, Calendar, and Drive
remain as existing API connectors; this package adds CLI-backed connectors
for services that benefit from the declarative YAML + Pydantic config model.
"""

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

__all__ = [
    # Connectors
    "SheetsConnector",
    "DocsConnector",
    "ChatConnector",
    "DriveConnector",
    # Sheets schemas
    "AppendRowsSchema",
    "UpdateCellsSchema",
    # Docs schemas
    "InsertTextSchema",
    "ReplaceTextSchema",
    # Chat schemas
    "SendMessageSchema",
    "CreateSpaceSchema",
    # Drive schemas
    "UploadFileSchema",
    "UpdateFileSchema",
    "DeleteFileSchema",
]


def __getattr__(name: str) -> object:
    """Lazy-load connector classes to avoid circular imports."""
    if name in ("SheetsConnector", "DocsConnector", "ChatConnector", "DriveConnector"):
        from nexus.backends.connectors.gws import connector

        return getattr(connector, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
