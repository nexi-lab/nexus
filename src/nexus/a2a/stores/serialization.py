"""Task serialization helpers.

Single source of truth for Task <-> dict and Task <-> DB row
conversions.  Extracted from ``database.py`` and ``in_memory.py``
to eliminate duplicated serialization logic (Decision 3 / #1586).
"""

import json
from typing import Any

from nexus.a2a.models import (
    Artifact,
    Message,
    Task,
    TaskState,
    TaskStatus,
)


def task_to_dict(task: Task) -> dict[str, Any]:
    """Serialize a Task to a plain dict (JSON-safe)."""
    return task.model_dump(mode="json")

def task_from_dict(data: dict[str, Any]) -> Task:
    """Deserialize a plain dict into a Task model."""
    return Task.model_validate(data)

def task_to_db_columns(task: Task) -> dict[str, Any]:
    """Convert a Task to DB column values.

    Returns a dict with keys: state, messages_json, artifacts_json,
    metadata_json.
    """
    return {
        "state": task.status.state.value,
        "messages_json": json.dumps([m.model_dump(mode="json") for m in task.history]),
        "artifacts_json": json.dumps([a.model_dump(mode="json") for a in task.artifacts]),
        "metadata_json": json.dumps(task.metadata) if task.metadata else None,
    }

def task_from_db_row(row: Any) -> Task:
    """Convert a database row to a Task model."""
    messages = json.loads(row.messages_json) if row.messages_json else []
    artifacts = json.loads(row.artifacts_json) if row.artifacts_json else []
    metadata = json.loads(row.metadata_json) if row.metadata_json else None

    return Task(
        id=row.id,
        contextId=row.context_id,
        status=TaskStatus(
            state=TaskState(row.state),
            timestamp=row.updated_at,
        ),
        history=[Message.model_validate(m) for m in messages],
        artifacts=[Artifact.model_validate(a) for a in artifacts],
        metadata=metadata,
    )
