"""Pydantic schemas for Google Workspace CLI connectors (Sheets, Docs, Chat).

Used by PathCLIBackend for write operation validation. Each schema defines
the YAML structure an agent writes to trigger a CLI operation.

Phase 3 (Issue #3148).
"""

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------


class AppendRowsSchema(BaseModel):
    """Schema for appending rows to a Google Sheet."""

    agent_intent: str = Field(..., min_length=10, description="Why rows are being appended")
    spreadsheet_id: str = Field(..., description="Spreadsheet ID or URL")
    sheet_name: str = Field(default="Sheet1", description="Target sheet tab name")
    values: list[list[str]] = Field(..., min_length=1, description="Rows to append (list of lists)")
    value_input_option: str = Field(
        default="USER_ENTERED",
        description="How to interpret input: RAW or USER_ENTERED",
    )
    confirm: bool = Field(default=False, description="Explicit confirmation")


class UpdateCellsSchema(BaseModel):
    """Schema for updating specific cells in a Google Sheet."""

    agent_intent: str = Field(..., min_length=10, description="Why cells are being updated")
    spreadsheet_id: str = Field(..., description="Spreadsheet ID")
    range: str = Field(..., description="A1 notation range (e.g., 'Sheet1!A1:B2')")
    values: list[list[str]] = Field(..., min_length=1, description="Cell values to set")
    confirm: bool = Field(default=False, description="Explicit confirmation")


# ---------------------------------------------------------------------------
# Google Docs
# ---------------------------------------------------------------------------


class InsertTextSchema(BaseModel):
    """Schema for inserting text into a Google Doc."""

    agent_intent: str = Field(..., min_length=10, description="Why text is being inserted")
    document_id: str = Field(..., description="Document ID or URL")
    text: str = Field(..., min_length=1, description="Text content to insert")
    location: str = Field(
        default="end",
        description="Where to insert: 'start', 'end', or character index",
    )
    confirm: bool = Field(default=False, description="Explicit confirmation")


class ReplaceTextSchema(BaseModel):
    """Schema for find-and-replace in a Google Doc."""

    agent_intent: str = Field(..., min_length=10, description="Why text is being replaced")
    document_id: str = Field(..., description="Document ID")
    find: str = Field(..., min_length=1, description="Text to find")
    replace: str = Field(..., description="Replacement text")
    match_case: bool = Field(default=True, description="Case-sensitive matching")
    confirm: bool = Field(default=False, description="Explicit confirmation")


# ---------------------------------------------------------------------------
# Google Chat
# ---------------------------------------------------------------------------


class SendMessageSchema(BaseModel):
    """Schema for sending a message in Google Chat."""

    agent_intent: str = Field(..., min_length=10, description="Why this message is being sent")
    space: str = Field(..., description="Chat space name or ID")
    text: str = Field(..., min_length=1, description="Message text (supports Chat markdown)")
    thread_key: str | None = Field(default=None, description="Thread key for threaded replies")
    user_confirmed: bool = Field(default=False, description="User confirmed sending (irreversible)")


class CreateSpaceSchema(BaseModel):
    """Schema for creating a Google Chat space."""

    agent_intent: str = Field(..., min_length=10, description="Why this space is being created")
    display_name: str = Field(..., min_length=1, max_length=128, description="Space display name")
    space_type: str = Field(default="SPACE", description="SPACE or GROUP_CHAT")
    confirm: bool = Field(default=False, description="Explicit confirmation")


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------


class UploadFileSchema(BaseModel):
    """Schema for uploading a file to Google Drive."""

    agent_intent: str = Field(..., min_length=10, description="Why this file is being uploaded")
    name: str = Field(..., min_length=1, max_length=1024, description="File name")
    parent_id: str | None = Field(
        default=None, description="Parent folder ID (root if not specified)"
    )
    mime_type: str | None = Field(
        default=None, description="MIME type (auto-detected if not specified)"
    )
    content_path: str | None = Field(
        default=None, description="Local path to file content to upload"
    )
    description: str = Field(default="", description="File description")
    confirm: bool = Field(default=False, description="Explicit confirmation")


class UpdateFileSchema(BaseModel):
    """Schema for updating a file's metadata in Google Drive."""

    agent_intent: str = Field(..., min_length=10, description="Why this file is being updated")
    file_id: str = Field(..., description="Drive file ID to update")
    name: str | None = Field(default=None, description="New file name")
    description: str | None = Field(default=None, description="New file description")
    parent_id: str | None = Field(default=None, description="Move to this parent folder")
    starred: bool | None = Field(default=None, description="Star/unstar the file")
    confirm: bool = Field(default=False, description="Explicit confirmation")


class DeleteFileSchema(BaseModel):
    """Schema for deleting a file from Google Drive."""

    agent_intent: str = Field(..., min_length=10, description="Why this file is being deleted")
    file_id: str = Field(..., description="Drive file ID to delete")
    user_confirmed: bool = Field(
        default=False, description="User confirmed deletion (moves to trash)"
    )
