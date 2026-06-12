"""Restart-survival regression test for issue #4343.

The cluster kernel must keep its VFS namespace (the global
``LocalMetaStore``) in a durable redb under ``NEXUS_DATA_DIR`` — not in
the ``tempfile::tempdir()`` the kernel boots with. Before the fix
(nexus-vfs rev ``32c4731`` and earlier) every kernel restart silently
dropped every file registration: payload bytes survived under
``<data_dir>/root/**`` while ``stat`` returned not-found for everything
written before the boot.

Spawns the real ``nexus-cluster`` binary twice on the same data dir:
write -> restart -> the path must still exist.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nexus.remote.kernel_client import KernelClient

pytestmark = pytest.mark.integration

PAYLOAD = b"#4343 restart survival probe"
TEST_PATH = "/restart-survival/probe.txt"


def _open_kernel(data_dir: Path) -> KernelClient:
    client = KernelClient(metadata_path=str(data_dir))
    try:
        client.open()
    except FileNotFoundError as exc:
        client.close()  # releases the spawn-attempt stderr tempfile
        if os.environ.get("NEXUS_KERNEL_BINARY"):
            # CI pinned an explicit binary — a missing/broken one is a
            # failure, not a skip, or the regression gate silently
            # turns itself off.
            raise
        pytest.skip(
            f"requires the ``nexus-cluster`` binary on PATH "
            f"(KernelClient spawns it). Build it in nexus-vfs with "
            f"``cargo build -p nexus-cluster`` and put "
            f"``target/debug`` on PATH. ({exc})"
        )
    except Exception:
        client.close()  # don't orphan a spawned kernel on boot failure
        raise
    return client


def test_namespace_survives_kernel_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # An ambient NEXUS_METASTORE_PATH would redirect both kernels to a
    # shared fixed-path metastore, defeating tmp_path isolation — a
    # previous run's registration could false-pass the constant probe
    # path. The kernel must exercise its <data_dir>/metastore.redb
    # default here.
    monkeypatch.delenv("NEXUS_METASTORE_PATH", raising=False)
    data_dir = tmp_path / "data"

    first = _open_kernel(data_dir)
    try:
        first.sys_write(TEST_PATH, data=PAYLOAD)
        assert first.sys_stat(TEST_PATH) is not None
    finally:
        first.close()

    # Default-path contract (#4343): the durable metastore lands at
    # <NEXUS_DATA_DIR>/metastore.redb, not a tempdir.
    assert (data_dir / "metastore.redb").exists(), (
        "kernel did not create the durable metastore at its default path"
    )

    second = _open_kernel(data_dir)
    try:
        # The registration must survive the restart (#4343: with the
        # tempdir-backed metastore this returned None while the payload
        # bytes sat byte-identical under <data_dir>/root/).
        assert second.sys_stat(TEST_PATH) is not None, (
            "VFS namespace lost across kernel restart — metastore is not durable (#4343)"
        )
        assert second.sys_read_raw(TEST_PATH) == PAYLOAD
        # Negative control: a never-written path must stay absent
        # (guards against walk-import-style blanket re-registration).
        assert second.sys_stat("/restart-survival/never-written.txt") is None
    finally:
        second.close()
