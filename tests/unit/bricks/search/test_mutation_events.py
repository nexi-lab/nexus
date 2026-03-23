from datetime import UTC, datetime
from types import SimpleNamespace

from nexus.bricks.search.mutation_events import (
    SearchMutationEvent,
    SearchMutationOp,
    extract_zone_id,
    strip_zone_prefix,
)


def test_strip_zone_prefix() -> None:
    assert strip_zone_prefix("/zone/root/docs/readme.md") == "/docs/readme.md"
    assert strip_zone_prefix("/docs/readme.md") == "/docs/readme.md"


def test_extract_zone_id() -> None:
    assert extract_zone_id("/zone/corp/docs/readme.md") == "corp"
    assert extract_zone_id("/docs/readme.md") == "root"


def test_from_operation_log_row_normalizes_rename_rows() -> None:
    created_at = datetime.now(UTC).replace(tzinfo=None)
    delete_row = SimpleNamespace(
        operation_id="op-1",
        operation_type="rename",
        zone_id="corp",
        path="/zone/corp/docs/old.md",
        new_path="/zone/corp/docs/new.md",
        created_at=created_at,
        sequence_number=10,
        change_type="delete",
    )
    upsert_row = SimpleNamespace(
        operation_id="op-2",
        operation_type="rename",
        zone_id="corp",
        path="/zone/corp/docs/new.md",
        new_path=None,
        created_at=created_at,
        sequence_number=11,
        change_type="upsert",
    )

    delete_event = SearchMutationEvent.from_operation_log_row(delete_row)
    upsert_event = SearchMutationEvent.from_operation_log_row(upsert_row)

    assert delete_event is not None
    assert delete_event.op == SearchMutationOp.DELETE
    assert delete_event.virtual_path == "/docs/old.md"

    assert upsert_event is not None
    assert upsert_event.op == SearchMutationOp.UPSERT
    assert upsert_event.virtual_path == "/docs/new.md"
