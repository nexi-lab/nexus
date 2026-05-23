"""Shared NexusFS factory for tests.

Federation wiring is intentionally omitted — unit tests must not start
gRPC servers (port contention with xdist workers).
"""

from __future__ import annotations


def make_test_nexus(
    tmp_path,
    *,
    backend=None,
    permissions=None,
    parsing=None,
    cache=None,
    memory=None,
    distributed=None,
    is_admin=False,
    record_store=None,
    use_raft=False,
    metadata_store=None,
    context=None,
):
    """Create a NexusFS instance for testing via factory (Issue #1801)."""
    from pathlib import Path

    from nexus.core.config import (
        DistributedConfig,
        ParseConfig,
        PermissionConfig,
    )
    from nexus.factory import create_nexus_fs

    tmp_path = Path(tmp_path)

    if permissions is None:
        permissions = PermissionConfig(enforce=False)
    if parsing is None:
        parsing = ParseConfig(auto_parse=False)
    if distributed is None:
        distributed = DistributedConfig(
            enable_events=False,
            enable_workflows=False,
        )

    if metadata_store is None:
        del use_raft
        metadata_store = tmp_path / "metastore.redb"

    if backend is None:
        from nexus.backends.storage.path_local import PathLocalBackend

        data_dir = Path(tmp_path) / "data"
        data_dir.mkdir(exist_ok=True)
        backend = PathLocalBackend(root_path=str(data_dir))

    from tests.testkit.auth import TEST_ADMIN_CONTEXT, TEST_CONTEXT

    _init_cred = (
        context if context is not None else (TEST_ADMIN_CONTEXT if is_admin else TEST_CONTEXT)
    )

    return create_nexus_fs(
        backend=backend,
        metadata_store=metadata_store,
        record_store=record_store,
        permissions=permissions,
        parsing=parsing,
        cache=cache,
        memory=memory,
        distributed=distributed,
        is_admin=is_admin,
        enabled_bricks=frozenset(),
        init_cred=_init_cred,
    )
