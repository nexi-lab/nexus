"""The thin syscall dispatcher carries ``projection_seq`` over RPC / gRPC (#4738).

``/api/nfs/{method}`` and the gRPC ``Call`` servicer both encode whatever
``_apply_result_adapter`` returns.  Legacy Tier 2 shapes stay byte-identical
when no observer confirmed a row; a confirmed sequence rides along.
"""

from __future__ import annotations

from nexus.server._kernel_syscall_dispatch import _apply_result_adapter


def test_legacy_shapes_unchanged_without_projection_seq() -> None:
    assert _apply_result_adapter("delete", {}, {}) == {"deleted": True}
    assert _apply_result_adapter("sys_unlink", {"projection_seq": None}, {}) == {"deleted": True}
    assert _apply_result_adapter("rename", None, {}) == {"renamed": True}
    assert _apply_result_adapter("mkdir", None, {}) == {"created": True}
    assert _apply_result_adapter("rmdir", {}, {}) == {"removed": True}


def test_confirmed_projection_seq_rides_along_on_legacy_shapes() -> None:
    assert _apply_result_adapter("delete", {"projection_seq": 5}, {}) == {
        "deleted": True,
        "projection_seq": 5,
    }
    assert _apply_result_adapter("sys_rename", {"projection_seq": 6}, {}) == {
        "renamed": True,
        "projection_seq": 6,
    }
    assert _apply_result_adapter("mkdir", {"path": "/d", "projection_seq": 7}, {}) == {
        "created": True,
        "projection_seq": 7,
    }
    assert _apply_result_adapter("rmdir", {"projection_seq": 8}, {}) == {
        "removed": True,
        "projection_seq": 8,
    }
    # A bool is not a sequence.
    assert _apply_result_adapter("delete", {"projection_seq": True}, {}) == {"deleted": True}


def test_write_result_dict_passes_projection_seq_through() -> None:
    wire = _apply_result_adapter(
        "write",
        {"content_id": "c", "version": 1, "gen": 1, "size": 3, "projection_seq": 11},
        {"content": "abc"},
    )
    assert wire["bytes_written"] == 3
    assert wire["projection_seq"] == 11
