"""Unit tests for A2A task serialization helpers.

TDD-first tests for the extracted serialization module.
"""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

from nexus.a2a.models import (
    Artifact,
    DataPart,
    FileContent,
    FilePart,
    Message,
    Task,
    TaskState,
    TaskStatus,
    TextPart,
)
from nexus.a2a.stores.serialization import (
    task_from_db_row,
    task_from_dict,
    task_to_db_columns,
    task_to_dict,
)

def _make_task(
    task_id: str = "t-1",
    state: TaskState = TaskState.SUBMITTED,
    metadata: dict[str, Any] | None = None,
    artifacts: list[Artifact] | None = None,
    history: list[Message] | None = None,
) -> Task:
    return Task(
        id=task_id,
        contextId="ctx-1",
        status=TaskStatus(state=state, timestamp=datetime(2025, 1, 1, tzinfo=UTC)),
        history=history or [Message(role="user", parts=[TextPart(text="hello")])],
        artifacts=artifacts or [],
        metadata=metadata,
    )

class TestTaskToDict:
    def test_roundtrip(self) -> None:
        task = _make_task()
        d = task_to_dict(task)
        restored = task_from_dict(d)
        assert restored.id == task.id
        assert restored.status.state == task.status.state
        assert len(restored.history) == len(task.history)

    def test_with_artifacts(self) -> None:
        artifacts = [
            Artifact(
                artifactId="art-1",
                name="result.json",
                parts=[DataPart(data={"key": "value"})],
            )
        ]
        task = _make_task(artifacts=artifacts)
        d = task_to_dict(task)
        restored = task_from_dict(d)
        assert len(restored.artifacts) == 1
        assert restored.artifacts[0].artifactId == "art-1"

    def test_empty_artifacts_roundtrip(self) -> None:
        task = _make_task(artifacts=[])
        d = task_to_dict(task)
        restored = task_from_dict(d)
        assert restored.artifacts == []

    def test_null_metadata_roundtrip(self) -> None:
        task = _make_task(metadata=None)
        d = task_to_dict(task)
        restored = task_from_dict(d)
        assert restored.metadata is None

    def test_metadata_preserved(self) -> None:
        task = _make_task(metadata={"priority": "high", "tags": [1, 2]})
        d = task_to_dict(task)
        restored = task_from_dict(d)
        assert restored.metadata == {"priority": "high", "tags": [1, 2]}

    def test_multiple_message_types_roundtrip(self) -> None:
        history = [
            Message(role="user", parts=[TextPart(text="analyze")]),
            Message(
                role="agent",
                parts=[
                    TextPart(text="done"),
                    FilePart(file=FileContent(name="out.txt", mimeType="text/plain")),
                    DataPart(data={"result": 42}),
                ],
            ),
        ]
        task = _make_task(history=history)
        d = task_to_dict(task)
        restored = task_from_dict(d)
        assert len(restored.history) == 2
        assert len(restored.history[1].parts) == 3

class TestDbColumns:
    def test_state_value(self) -> None:
        task = _make_task(state=TaskState.WORKING)
        cols = task_to_db_columns(task)
        assert cols["state"] == "working"

    def test_from_db_row_with_metadata(self) -> None:
        row = MagicMock()
        row.id = "t-1"
        row.context_id = "ctx-1"
        row.state = "completed"
        row.messages_json = '[{"role": "user", "parts": [{"type": "text", "text": "hi"}]}]'
        row.artifacts_json = "[]"
        row.metadata_json = '{"key": "val"}'
        row.updated_at = datetime(2025, 1, 1, tzinfo=UTC)

        task = task_from_db_row(row)
        assert task.id == "t-1"
        assert task.status.state == TaskState.COMPLETED
        assert task.metadata == {"key": "val"}

    def test_from_db_row_null_metadata(self) -> None:
        row = MagicMock()
        row.id = "t-2"
        row.context_id = "ctx-2"
        row.state = "submitted"
        row.messages_json = "[]"
        row.artifacts_json = "[]"
        row.metadata_json = None
        row.updated_at = datetime(2025, 1, 1, tzinfo=UTC)

        task = task_from_db_row(row)
        assert task.metadata is None
