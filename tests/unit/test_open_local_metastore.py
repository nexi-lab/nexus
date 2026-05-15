"""Unit test for ``nexus._open_local_metastore`` sentinel handling.

Pins the cross-platform contract that the SQLite-style ``":memory:"``
sentinel skips on-disk path construction.  Without this branch,
``Path(":memory:").with_suffix(".redb")`` yields ``:memory:.redb`` —
syntactically invalid on Windows because the colon is parsed as a
drive separator, so ``str(_redb_path)`` raises ``IOError`` inside redb.
"""

from __future__ import annotations

import nexus


def test_open_local_metastore_memory_sentinel_returns_proxy() -> None:
    """``:memory:`` must yield a working proxy on every platform.

    Regression pin: PR #3996 follow-up.  Before the fix this raised
    ``OSError: LocalMetaStore: IOError(":memory:.redb": ...)`` on
    Windows because ``Path(":memory:").with_suffix(".redb")`` produces
    a colon-laden path that Windows rejects.
    """
    proxy = nexus._open_local_metastore(":memory:")
    assert proxy is not None
    # In-memory mode means no on-disk redb path was constructed —
    # the proxy is wired against ``PyKernel::new()``'s default
    # ``MemoryMetastore``.
