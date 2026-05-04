"""Lightweight boot helpers for archive integration tests (#3793).

Mirrors the pattern used in ``tests/conftest.py::make_test_nexus`` — boots a
real NexusFS instance backed by a fresh PathLocalBackend / RustMetastoreProxy
without the full server stack.

Public API:
    boot_lightweight_nexus(db_path) -> NexusFS
    plant_secret_doc(fs, zone_id) -> None
    plant_provider_key(fs, provider_name, key) -> None
    plant_timeline_corpus(fs, zone_id) -> None
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def boot_lightweight_nexus(db_path: Path) -> Any:
    """Boot a NexusFS suitable for archive integration tests.

    Avoids docker / server stack. Uses the same factory path as
    ``make_test_nexus`` in ``tests/conftest.py`` with:
    - PathLocalBackend rooted at ``db_path.parent / "data"``
    - RustMetastoreProxy backed by a redb file at ``db_path``
    - Permissions disabled (enforce=False) for frictionless fixture writes
    - Auto-parse disabled

    Args:
        db_path: Path for the metadata store redb file.

    Returns:
        Booted ``NexusFS`` instance. Caller is responsible for calling
        ``fs.shutdown()`` (or using as a context manager).
    """
    from nexus.backends.storage.path_local import PathLocalBackend
    from nexus.core.config import DistributedConfig, ParseConfig, PermissionConfig
    from nexus.factory import create_nexus_fs

    db_path = Path(db_path)
    data_dir = db_path.parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    backend = PathLocalBackend(root_path=str(data_dir))

    from nexus_runtime import PyKernel

    kernel = PyKernel()
    try:
        import nexus_runtime as _nk

        _nk.install_transport_wiring(kernel)
        _nk.install_federation_wiring(kernel)
    except Exception:
        pass

    metadata_store = kernel.set_metastore_path(str(db_path)) or kernel

    from tests.helpers.test_context import TEST_ADMIN_CONTEXT

    return create_nexus_fs(
        backend=backend,
        metadata_store=metadata_store,
        permissions=PermissionConfig(enforce=False),
        parsing=ParseConfig(auto_parse=False),
        distributed=DistributedConfig(
            enable_events=False,
            enable_workflows=False,
        ),
        is_admin=True,
        enabled_bricks=frozenset(),
        init_cred=TEST_ADMIN_CONTEXT,
    )


def plant_secret_doc(fs: Any, zone_id: str) -> None:
    """Write a document whose body contains an Anthropic API key.

    Used by ``test_planted_secrets.py`` to verify that the regex-stripper
    backstop redacts free-text credentials before they reach the bundle.

    Args:
        fs: Booted NexusFS instance.
        zone_id: Zone prefix to write under (used as a path component).
    """
    body = b"The AI key is sk-ant-aaaaaaaaaaaaaaaaaaaa - keep it secret!"
    fs.write(
        f"/{zone_id}/secret_doc.txt",
        body,
        context=fs._init_cred,
    )


def plant_provider_key(fs: Any, provider_name: str, api_key: str) -> None:
    """Write a fake provider-table row under the zone's metadata path.

    The export service's SchemaStripper recognises the "providers" table
    pattern and replaces ``api_key`` values with ``${PROVIDER_KEY_<name>}``.
    We simulate a provider row by writing a JSON file to a well-known path
    that the integration test can later inspect inside the bundle.

    Args:
        fs: Booted NexusFS instance.
        provider_name: Provider name (e.g. "anthropic").
        api_key: Fake API key value to embed.
    """
    row = json.dumps({"name": provider_name, "api_key": api_key})
    fs.write(
        f"/providers/{provider_name}.json",
        row.encode(),
        context=fs._init_cred,
    )


def plant_timeline_corpus(fs: Any, zone_id: str) -> None:
    """Write several documents with staggered timestamps.

    Creates three documents:
    - ``early.txt``:  2026-03-15 (before the audit window)
    - ``in_window.txt``: 2026-04-15 (inside 2026-04-01..2026-05-01)
    - ``late.txt``:   2026-05-10 (after the audit window)

    The timestamps are embedded in the file *content* because the metadata
    store's ``modified_at`` field is set by the kernel at write time (not
    by the caller).  The audit-window test inspects file *paths* in the
    bundle rather than modified_at values.

    Args:
        fs: Booted NexusFS instance.
        zone_id: Zone prefix to write under.
    """
    docs = [
        (f"/{zone_id}/early.txt", b"early doc 2026-03-15"),
        (f"/{zone_id}/in_window.txt", b"in-window doc 2026-04-15"),
        (f"/{zone_id}/late.txt", b"late doc 2026-05-10"),
    ]
    for path, content in docs:
        fs.write(path, content, context=fs._init_cred)


__all__ = [
    "boot_lightweight_nexus",
    "plant_provider_key",
    "plant_secret_doc",
    "plant_timeline_corpus",
]
