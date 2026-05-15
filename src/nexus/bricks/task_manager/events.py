"""Task Manager event handler protocol.

The write hook parses task JSON and delivers signal dicts directly to
DT_PIPE.  No custom event dataclasses — the kernel FileEvent already
records the mutation; dispatch signals carry only the parsed payload.
"""

from __future__ import annotations

from typing import Any, Protocol


class TaskSignalHandler(Protocol):
    """Protocol for objects that react to task lifecycle signals."""

    def on_task_signal(self, signal_type: str, payload: dict[str, Any]) -> None: ...
