from __future__ import annotations

from nexus.core.file_events import FileEvent, FileEventType


def test_file_event_round_trips_generation() -> None:
    event = FileEvent(
        type=FileEventType.FILE_WRITE,
        path="/docs/a.txt",
        version=3,
        gen=7,
    )

    encoded = event.to_dict()
    restored = FileEvent.from_dict(encoded)

    assert encoded["gen"] == 7
    assert restored.gen == 7
