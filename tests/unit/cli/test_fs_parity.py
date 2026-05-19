"""CLI <-> RPC <-> syscall parity for the core FS surface (Issue #4133)."""

from __future__ import annotations


def test_inproc_fixture_roundtrips(inproc_nexus):
    nx = inproc_nexus
    nx.write("/a.txt", b"hello")
    assert nx.read("/a.txt") == b"hello"
    st = nx.stat("/a.txt")
    assert st["size"] == 5 and "content_id" in st
