"""CLI <-> RPC <-> syscall parity for the core FS surface (Issue #4133)."""

from __future__ import annotations

import json

from click.testing import CliRunner


def test_inproc_fixture_roundtrips(inproc_nexus):
    nx = inproc_nexus
    nx.write("/a.txt", b"hello")
    assert nx.read("/a.txt") == b"hello"
    st = nx.stat("/a.txt")
    assert st["size"] == 5 and "content_id" in st


# ---------------------------------------------------------------------------
# Task 2: nexus stat (stat / stat_bulk)
# ---------------------------------------------------------------------------


def test_stat_single_parity(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import stat_cmd

    nx = patched_fs
    nx.write("/s.txt", b"abcde")
    rpc = nx.stat("/s.txt")
    res = cli_runner.invoke(stat_cmd, ["/s.txt", "--json"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)["data"]
    assert out["size"] == rpc["size"] == 5
    assert out["content_id"] == rpc["content_id"]


def test_stat_multi_uses_stat_bulk(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import stat_cmd

    nx = patched_fs
    nx.write("/a.txt", b"aa")
    nx.write("/b.txt", b"bbb")
    rpc = nx.stat_bulk(["/a.txt", "/b.txt"])
    res = cli_runner.invoke(stat_cmd, ["/a.txt", "/b.txt", "--json"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)["data"]
    assert out["/a.txt"]["size"] == rpc["/a.txt"]["size"] == 2
    assert out["/b.txt"]["size"] == 3


# ---------------------------------------------------------------------------
# Task 3: nexus metadata (metadata_batch)
# ---------------------------------------------------------------------------


def test_metadata_extended_parity(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import metadata_cmd

    nx = patched_fs
    nx.write("/m.txt", b"hi")
    rpc = nx.metadata_batch(["/m.txt", "/nope.txt"])
    res = cli_runner.invoke(metadata_cmd, ["/m.txt", "/nope.txt", "--json"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)["data"]
    assert out["/m.txt"]["size"] == rpc["/m.txt"]["size"] == 2
    # metadata_batch carries the extended keys stat_bulk lacks:
    assert "mime_type" in out["/m.txt"] and "created_at" in out["/m.txt"]
    assert out["/nope.txt"] is None


# ---------------------------------------------------------------------------
# Task 4: nexus exists (exists_batch)
# ---------------------------------------------------------------------------


def test_exists_batch_parity_and_exit(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import exists_cmd

    nx = patched_fs
    nx.write("/here.txt", b"x")
    rpc = nx.exists_batch(["/here.txt", "/gone.txt"])
    assert rpc == {"/here.txt": True, "/gone.txt": False}
    # --json: full map, exit 0
    res = cli_runner.invoke(exists_cmd, ["/here.txt", "/gone.txt", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["data"] == {"/here.txt": True, "/gone.txt": False}
    # plain: exit 0 iff ALL exist
    assert cli_runner.invoke(exists_cmd, ["/here.txt"]).exit_code == 0
    assert cli_runner.invoke(exists_cmd, ["/here.txt", "/gone.txt"]).exit_code == 1


# ---------------------------------------------------------------------------
# Task 5: nexus read-bulk (read_bulk / read_batch)
# ---------------------------------------------------------------------------


def test_read_bulk_parity(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import read_bulk_cmd

    nx = patched_fs
    nx.write("/r1.txt", b"one")
    nx.write("/r2.txt", b"two")
    rpc = nx.read_bulk(["/r1.txt", "/r2.txt"])
    res = cli_runner.invoke(read_bulk_cmd, ["/r1.txt", "/r2.txt", "--json"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)["data"]
    assert out["/r1.txt"] == rpc["/r1.txt"].decode() == "one"
    assert out["/r2.txt"] == "two"


def test_read_bulk_atomic_raises_on_missing(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import read_bulk_cmd

    nx = patched_fs
    nx.write("/r1.txt", b"one")
    res = cli_runner.invoke(read_bulk_cmd, ["/r1.txt", "/missing.txt", "--atomic", "--json"])
    # read_batch(partial=False) raises -> CLI catches and exits 1
    assert res.exit_code == 1


# ---------------------------------------------------------------------------
# Task 6: nexus rename-batch (rename_batch — per-item independent)
# ---------------------------------------------------------------------------


def test_rename_batch_per_item_independent(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import rename_batch_cmd

    nx = patched_fs
    nx.write("/old1.txt", b"1")  # /old2.txt deliberately absent
    res = cli_runner.invoke(
        rename_batch_cmd,
        ["/old1.txt:/new1.txt", "/old2.txt:/new2.txt", "--json"],
    )
    assert res.exit_code == 0, res.output  # independent: one failure does not abort the rest
    out = json.loads(res.output)["data"]
    assert out["/old1.txt"]["success"] is True
    assert out["/old1.txt"]["new_path"] == "/new1.txt"
    assert out["/old2.txt"]["success"] is False
    assert nx.read("/new1.txt") == b"1"


# ---------------------------------------------------------------------------
# Task 7: nexus rm-batch (delete_batch — per-item independent)
# ---------------------------------------------------------------------------


def test_rm_batch_per_item_independent(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import rm_batch_cmd

    nx = patched_fs
    nx.write("/d1.txt", b"1")
    nx.write("/d2.txt", b"2")
    res = cli_runner.invoke(rm_batch_cmd, ["/d1.txt", "/missing.txt", "/d2.txt", "--json"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)["data"]
    assert out["/d1.txt"]["success"] is True
    assert out["/d2.txt"]["success"] is True
    assert out["/missing.txt"]["success"] is False
    assert not nx.access("/d1.txt")


# ---------------------------------------------------------------------------
# Task 8: nexus cat --offset / --length (read_range)
# ---------------------------------------------------------------------------


def test_cat_range_equals_slice(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import cat

    nx = patched_fs
    nx.write("/big.txt", b"0123456789")
    assert nx.read_range("/big.txt", 2, 5) == b"234"
    res = cli_runner.invoke(cat, ["/big.txt", "--offset", "2", "--length", "3"])
    assert res.exit_code == 0, res.output
    assert res.output.rstrip("\n") == "234" or res.stdout_bytes == b"234"


def test_cat_no_range_unchanged(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import cat

    nx = patched_fs
    nx.write("/whole.txt", b"hello world")
    res = cli_runner.invoke(cat, ["/whole.txt"])
    assert res.exit_code == 0, res.output
    assert "hello world" in res.output


# ---------------------------------------------------------------------------
# Task 9: cat --stream / write --stream
# ---------------------------------------------------------------------------


def test_cat_stream_matches_full(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import cat

    nx = patched_fs
    body = b"x" * 200_000
    nx.write("/strm.bin", body)
    res = cli_runner.invoke(cat, ["/strm.bin", "--stream", "--chunk-size", "65536"])
    assert res.exit_code == 0, res.output
    assert res.stdout_bytes == body


def test_write_stream_from_stdin(patched_fs, cli_runner: CliRunner):
    from nexus.cli.commands.file_ops import write

    nx = patched_fs
    res = cli_runner.invoke(write, ["/ws.txt", "--stream"], input="streamed-bytes")
    assert res.exit_code == 0, res.output
    assert nx.read("/ws.txt") == b"streamed-bytes"


# ---------------------------------------------------------------------------
# Task 10: nexus admin fs (backfill-index / flush-write-observer)
# ---------------------------------------------------------------------------


def test_admin_fs_flush_and_backfill(inproc_nexus, cli_runner: CliRunner, monkeypatch):
    """admin fs * runs the admin-only RPCs against the in-process FS."""
    import contextlib

    @contextlib.asynccontextmanager
    async def _open(*a, **k):
        yield inproc_nexus

    monkeypatch.setattr("nexus.cli.utils.open_filesystem", _open, raising=False)
    from nexus.cli.commands.admin import admin

    r1 = cli_runner.invoke(admin, ["fs", "flush-write-observer", "--json"])
    assert r1.exit_code == 0, r1.output
    assert "flushed" in json.loads(r1.output)["data"]

    r2 = cli_runner.invoke(admin, ["fs", "backfill-index", "/", "--json"])
    assert r2.exit_code == 0, r2.output
    assert "entries_created" in json.loads(r2.output)["data"]
