"""Regression tests for the documented local quickstart path."""

from __future__ import annotations

from pathlib import Path

import nexus


def test_local_connect_source_checkout_quickstart(
    tmp_path: Path,
) -> None:
    """A source checkout should still support the local SDK quickstart."""

    nx = nexus.connect(
        config={
            "profile": "embedded",
            "data_dir": str(tmp_path / "nexus-data"),
        }
    )
    try:
        nx.write("/hello.txt", b"hello")
        assert nx.sys_read("/hello.txt") == b"hello"
    finally:
        nx.close()
