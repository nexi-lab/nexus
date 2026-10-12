"""SDK path scope reaches Search as one query over the prefix union."""

from __future__ import annotations

from typing import Any

import pytest

from nexus.contracts.search_types import SearchRequest, split_path_scope


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, (None, ())),
        ("", (None, ())),
        ("/ws/documents/", ("/ws/documents/", ())),
        (["/ws/documents/"], ("/ws/documents/", ())),
        (["/ws/documents/", "", "/ws/documents/"], ("/ws/documents/", ())),
        (
            ["/ws/documents/", "/ws/notes/", "/ws/documents/"],
            (None, ("/ws/documents/", "/ws/notes/")),
        ),
    ],
)
def test_split_path_scope(raw: Any, expected: tuple[str | None, tuple[str, ...]]) -> None:
    assert split_path_scope(raw) == expected


def test_daemon_maps_path_filters_onto_the_proto() -> None:
    from nexus.bricks.search.daemon import _request_to_pb

    pb = _request_to_pb(
        SearchRequest(query="q", path_filters=("/ws/documents/", "/ws/notes/")),
        chunks_per_page=0,
    )
    assert pb.path_filter == ""
    assert list(pb.path_filters) == ["/ws/documents/", "/ws/notes/"]
