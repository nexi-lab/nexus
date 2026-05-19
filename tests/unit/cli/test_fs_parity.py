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
