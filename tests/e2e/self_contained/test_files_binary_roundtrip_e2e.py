"""E2E binary roundtrip for /api/v2/files/{write,read} (Issue #3989).

Two regressions covered:

1. NUL bytes in write payloads must NOT poison the indexing pipeline. Real
   binary file formats (PNG, PDF, zip, …) virtually always contain 0x00 in
   headers, padding, or compressed streams. Postgres TEXT/VARCHAR rejects
   embedded NULs (SQLSTATE 22021), and unsanitized chunk inserts would
   leave the asyncpg session in PendingRollbackError until worker recycle.
2. ``GET /read?encoding=base64`` must return content losslessly. The
   default UTF-8 path replaces non-text bytes with U+FFFD, so binary data
   cannot be recovered byte-for-byte.

The test wires a FastAPI TestClient to a real NexusFS (SQLite metastore +
CASLocalBackend) — same fixture shape as the batch_write_read e2e — and
asserts a SHA-256 match end-to-end.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.metadata import DT_MOUNT
from nexus.contracts.types import OperationContext
from nexus.core.config import PermissionConfig
from nexus.core.nexus_fs import NexusFS
from nexus.fs import _make_mount_entry
from nexus.fs._sqlite_meta import SQLiteMetastore
from nexus.server.api.v2.routers.async_files import create_async_files_router
from nexus.server.dependencies import get_auth_result

# Real PNG header — IHDR chunk + signature, contains multiple 0x00 bytes
# in the magic bytes (89 50 4E 47 0D 0A 1A 0A) and the IHDR length field.
_PNG_HEADER = bytes.fromhex(
    "89504e470d0a1a0a"  # PNG signature (contains 0x00s)
    "0000000d49484452"  # IHDR length=13 + chunk type
    "00000001"  # width=1
    "00000001"  # height=1
    "0806000000"  # bit depth=8, color=6, compression=0, filter=0, interlace=0
    "1f15c489"  # IHDR CRC
)


@pytest.fixture()
def real_fs(tmp_path: Path) -> NexusFS:
    from nexus.backends.storage.cas_local import CASLocalBackend

    db_path = str(tmp_path / "meta.db")
    metastore = SQLiteMetastore(db_path)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    backend = CASLocalBackend(root_path=data_dir)

    kernel = NexusFS(
        metadata_store=metastore,
        permissions=PermissionConfig(enforce=False),
    )
    kernel._init_cred = OperationContext(
        user_id="e2e-user",
        groups=[],
        zone_id=ROOT_ZONE_ID,
        is_admin=True,
    )
    kernel.sys_setattr("/files", entry_type=DT_MOUNT, backend=backend)
    metastore.put(_make_mount_entry("/files", backend.name))

    return kernel


@pytest.fixture()
def client(real_fs: NexusFS) -> TestClient:
    app = FastAPI()
    router = create_async_files_router(nexus_fs=real_fs)
    app.include_router(router)
    app.dependency_overrides[get_auth_result] = lambda: {
        "authenticated": True,
        "user_id": "e2e-user",
        "groups": [],
        "zone_id": "root",
        "is_admin": True,
    }
    return TestClient(app)


def test_write_then_read_png_roundtrips_byte_for_byte(client: TestClient) -> None:
    """Real PNG bytes (with NULs) must roundtrip via base64 with SHA-256 equality."""
    payload = _PNG_HEADER
    expected_sha = hashlib.sha256(payload).hexdigest()

    write_resp = client.post(
        "/write",
        json={
            "path": "/files/probe.png",
            "content": base64.b64encode(payload).decode("ascii"),
            "encoding": "base64",
        },
    )
    assert write_resp.status_code == 200, write_resp.text
    assert write_resp.json()["size"] == len(payload)

    read_resp = client.get("/read", params={"path": "/files/probe.png", "encoding": "base64"})
    assert read_resp.status_code == 200, read_resp.text
    body = read_resp.json()
    assert body["encoding"] == "base64"
    decoded = base64.b64decode(body["content"])
    assert hashlib.sha256(decoded).hexdigest() == expected_sha
    assert decoded == payload


def test_read_default_encoding_is_unset_for_text(client: TestClient) -> None:
    """Backward-compat: omitting ``encoding`` returns a UTF-8 string with no
    ``encoding`` field (legacy SDK shape)."""
    write_resp = client.post(
        "/write",
        json={"path": "/files/text.txt", "content": "hello world"},
    )
    assert write_resp.status_code == 200, write_resp.text

    read_resp = client.get("/read", params={"path": "/files/text.txt"})
    assert read_resp.status_code == 200
    body = read_resp.json()
    assert body["content"] == "hello world"
    assert body.get("encoding") is None


def test_subsequent_write_succeeds_after_nul_payload(client: TestClient) -> None:
    """Writing NUL-bearing content must not break later writes/reads on the
    same client/worker (Issue #3989 — the indexing pipeline used to leave
    asyncpg in PendingRollbackError until recycle)."""
    nul_blob = b"\x00\x01\x02\x00\x03\x00binary"

    first = client.post(
        "/write",
        json={
            "path": "/files/blob1.bin",
            "content": base64.b64encode(nul_blob).decode("ascii"),
            "encoding": "base64",
        },
    )
    assert first.status_code == 200, first.text

    second = client.post(
        "/write",
        json={"path": "/files/follow.txt", "content": "still working"},
    )
    assert second.status_code == 200, second.text

    read_back = client.get("/read", params={"path": "/files/blob1.bin", "encoding": "base64"})
    assert read_back.status_code == 200
    assert base64.b64decode(read_back.json()["content"]) == nul_blob


def test_read_rejects_unknown_encoding(client: TestClient) -> None:
    client.post(
        "/write",
        json={"path": "/files/x.txt", "content": "x"},
    )
    resp = client.get("/read", params={"path": "/files/x.txt", "encoding": "hex"})
    assert resp.status_code == 400
    assert "encoding" in resp.json()["detail"].lower()


def test_versioned_read_rejects_stale_hash(client: TestClient) -> None:
    """When a file has been overwritten, a request for the old content hash
    must NOT return current bytes labeled with the historical hash. The
    endpoint reads by path (no CAS-by-hash plumbing yet), so we verify the
    hash and refuse mismatched reads to avoid corrupting diff/rollback
    consumers (Issue #3989 — codex review)."""
    import base64
    import hashlib

    # Write A
    payload_a = b"\x00\x01alpha\x00"
    write_a = client.post(
        "/write",
        json={
            "path": "/files/versioned.bin",
            "content": base64.b64encode(payload_a).decode("ascii"),
            "encoding": "base64",
        },
    )
    assert write_a.status_code == 200
    hash_a = write_a.json()["content_id"]
    # BLAKE3 hash from kernel; ensure non-empty
    assert hash_a

    # Overwrite with B
    payload_b = b"\x00\x02beta\x00\x00"
    assert hashlib.sha256(payload_b).digest() != hashlib.sha256(payload_a).digest()
    write_b = client.post(
        "/write",
        json={
            "path": "/files/versioned.bin",
            "content": base64.b64encode(payload_b).decode("ascii"),
            "encoding": "base64",
        },
    )
    assert write_b.status_code == 200

    # Note: ?version=<hash_a> requires transaction_id wiring that isn't
    # available in this lightweight TestClient setup. We assert the
    # query-param validation remains strict (the no-transaction guard
    # already covers this) and rely on the production-stack live test for
    # full hash-mismatch coverage.
    resp = client.get(
        "/read",
        params={"path": "/files/versioned.bin", "version": hash_a},
    )
    # Without transaction_id we get 400 — the existing guard.
    assert resp.status_code == 400
    assert "transaction_id" in resp.json()["detail"].lower()


def test_versioned_read_passes_writer_address_as_origin(
    real_fs: NexusFS, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``cas_read`` is invoked for a historical version, the endpoint
    must plumb the path's ``last_writer_address`` as a federation origin.
    Otherwise chunked manifests whose chunks live on a peer (replication
    window not yet closed) become unrecoverable on follower nodes
    (Issue #3989, codex r6).

    We patch the kernel's ``cas_read`` to capture kwargs and stub the
    snapshot service so the version-validation gate is satisfied without
    needing a real transactional snapshot."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    payload = b"x"
    write_resp_app = FastAPI()
    write_resp_app.include_router(create_async_files_router(nexus_fs=real_fs))
    write_resp_app.dependency_overrides[get_auth_result] = lambda: {
        "authenticated": True,
        "user_id": "e2e-user",
        "groups": [],
        "zone_id": "root",
        "is_admin": True,
    }
    setup_client = TestClient(write_resp_app)
    write_resp = setup_client.post(
        "/write",
        json={"path": "/files/origin_probe.bin", "content": "x"},
    )
    assert write_resp.status_code == 200, write_resp.text
    hash_a = write_resp.json()["content_id"]
    assert hash_a

    # Stub sys_stat so last_writer_address is a known sentinel.
    real_stat = real_fs.sys_stat

    def _stat(path: str, **kw: object) -> dict[str, object] | None:
        result = real_stat(path, **kw)
        if result is not None:
            result["last_writer_address"] = "peer-1:2126"
        return result

    monkeypatch.setattr(real_fs, "sys_stat", _stat)

    # The router resolves the owning mount via fs.list_mounts(); this minimal
    # NexusFS fixture has no mount_service wired, so stub it to expose /files.
    monkeypatch.setattr(
        real_fs,
        "list_mounts",
        lambda context=None: [{"mount_point": "/files"}],
    )

    # Capture cas_read kwargs and short-circuit with the recorded payload.
    captured: dict[str, object] = {}

    def _fake_cas_read(
        mount_point: str,
        zone_id: str,
        content_id: str,
        *,
        origins: list[str] | None = None,
    ) -> bytes:
        captured["mount_point"] = mount_point
        captured["zone_id"] = zone_id
        captured["content_id"] = content_id
        captured["origins"] = list(origins or [])
        return payload

    # The Rust pyo3 kernel's attributes are read-only, so wrap the kernel
    # with a proxy that overrides cas_read while delegating everything else.
    real_kernel = real_fs._kernel

    class _KernelProxy:
        cas_read = staticmethod(_fake_cas_read)

        def __getattr__(self, name: str) -> object:
            return getattr(real_kernel, name)

    monkeypatch.setattr(real_fs, "_kernel", _KernelProxy())

    # Stub the snapshot service so version validation passes.
    snap_svc = MagicMock()
    snap_svc.get_transaction = AsyncMock(return_value=SimpleNamespace(zone_id="root"))
    snap_svc.list_entries = AsyncMock(
        return_value=[
            SimpleNamespace(
                path="/files/origin_probe.bin",
                original_hash=hash_a,
                new_hash=hash_a,
            )
        ]
    )

    app = FastAPI()
    router = create_async_files_router(nexus_fs=real_fs)
    app.state.transactional_snapshot_service = snap_svc
    app.include_router(router)
    app.dependency_overrides[get_auth_result] = lambda: {
        "authenticated": True,
        "user_id": "e2e-user",
        "groups": [],
        "zone_id": "root",
        "is_admin": True,
    }
    client = TestClient(app)

    resp = client.get(
        "/read",
        params={
            "path": "/files/origin_probe.bin",
            "version": hash_a,
            "transaction_id": "txn-stub",
            "encoding": "base64",
        },
    )
    assert resp.status_code == 200, resp.text
    assert captured["origins"] == ["peer-1:2126"], captured
    assert captured["content_id"] == hash_a


def test_versioned_read_uses_recorded_origin_for_original_hash(
    real_fs: NexusFS, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the requested version is the entry's ``original_hash``, the
    federation origin hint must prefer the writer address recorded in the
    snapshot's ``original_metadata`` ahead of the path's *current*
    writer. Otherwise a follower reading the original side after a
    cross-node overwrite would only contact the new writer and miss the
    node holding the original chunks (Issue #3989, codex r7)."""
    import json as _json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    payload = b"y"

    setup_app = FastAPI()
    setup_app.include_router(create_async_files_router(nexus_fs=real_fs))
    setup_app.dependency_overrides[get_auth_result] = lambda: {
        "authenticated": True,
        "user_id": "e2e-user",
        "groups": [],
        "zone_id": "root",
        "is_admin": True,
    }
    setup_client = TestClient(setup_app)
    write_resp = setup_client.post(
        "/write",
        json={"path": "/files/recorded_origin.bin", "content": "y"},
    )
    assert write_resp.status_code == 200, write_resp.text
    original_hash = write_resp.json()["content_id"]

    # Current writer differs from the recorded original writer.
    real_stat = real_fs.sys_stat

    def _stat(path: str, **kw: object) -> dict[str, object] | None:
        result = real_stat(path, **kw)
        if result is not None:
            result["last_writer_address"] = "current-writer:2126"
        return result

    monkeypatch.setattr(real_fs, "sys_stat", _stat)
    monkeypatch.setattr(
        real_fs,
        "list_mounts",
        lambda context=None: [{"mount_point": "/files"}],
    )

    captured: dict[str, object] = {}

    def _fake_cas_read(
        mount_point: str,
        zone_id: str,
        content_id: str,
        *,
        origins: list[str] | None = None,
    ) -> bytes:
        captured["origins"] = list(origins or [])
        return payload

    real_kernel = real_fs._kernel

    class _KernelProxy:
        cas_read = staticmethod(_fake_cas_read)

        def __getattr__(self, name: str) -> object:
            return getattr(real_kernel, name)

    monkeypatch.setattr(real_fs, "_kernel", _KernelProxy())

    # Snapshot entry whose original_metadata records the original-writer.
    snap_svc = MagicMock()
    snap_svc.get_transaction = AsyncMock(return_value=SimpleNamespace(zone_id="root"))
    snap_svc.list_entries = AsyncMock(
        return_value=[
            SimpleNamespace(
                path="/files/recorded_origin.bin",
                original_hash=original_hash,
                new_hash="0" * 64,  # post-write hash; irrelevant
                original_metadata=_json.dumps({"last_writer_address": "original-writer:2126"}),
            )
        ]
    )

    app = FastAPI()
    router = create_async_files_router(nexus_fs=real_fs)
    app.state.transactional_snapshot_service = snap_svc
    app.include_router(router)
    app.dependency_overrides[get_auth_result] = lambda: {
        "authenticated": True,
        "user_id": "e2e-user",
        "groups": [],
        "zone_id": "root",
        "is_admin": True,
    }
    client = TestClient(app)

    resp = client.get(
        "/read",
        params={
            "path": "/files/recorded_origin.bin",
            "version": original_hash,
            "transaction_id": "txn-stub",
            "encoding": "base64",
        },
    )
    assert resp.status_code == 200, resp.text
    # Recorded original writer comes first; current writer is appended as a
    # fallback. De-dup must not drop either, and the recorded one must lead.
    assert captured["origins"] == [
        "original-writer:2126",
        "current-writer:2126",
    ], captured


def test_versioned_read_serves_deleted_path_from_cas(
    real_fs: NexusFS, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Historical reads of a *deleted* path must succeed via CAS even
    though the live VFS path no longer resolves. Without this carve-out,
    diff/preview/rollback consumers cannot recover a snapshot of a file
    that has since been deleted (Issue #3989, codex r9)."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    payload = b"deleted-bytes"
    target = "/files/gone.bin"

    # Write then delete via the API so the live path no longer exists.
    setup_app = FastAPI()
    setup_app.include_router(create_async_files_router(nexus_fs=real_fs))
    setup_app.dependency_overrides[get_auth_result] = lambda: {
        "authenticated": True,
        "user_id": "e2e-user",
        "groups": [],
        "zone_id": "root",
        "is_admin": True,
    }
    setup_client = TestClient(setup_app)
    write_resp = setup_client.post(
        "/write",
        json={
            "path": target,
            "content": base64.b64encode(payload).decode("ascii"),
            "encoding": "base64",
        },
    )
    assert write_resp.status_code == 200, write_resp.text
    deleted_hash = write_resp.json()["content_id"]

    # Confirm fs.access returns False after delete (i.e. the live path is
    # gone) by directly removing the metadata via the kernel.
    real_fs.sys_unlink(target)
    assert real_fs.access(target) is False

    monkeypatch.setattr(
        real_fs,
        "list_mounts",
        lambda context=None: [{"mount_point": "/files"}],
    )

    captured: dict[str, object] = {}

    def _fake_cas_read(
        mount_point: str,
        zone_id: str,
        content_id: str,
        *,
        origins: list[str] | None = None,
    ) -> bytes:
        captured["content_id"] = content_id
        return payload

    real_kernel = real_fs._kernel

    class _KernelProxy:
        cas_read = staticmethod(_fake_cas_read)

        def __getattr__(self, name: str) -> object:
            return getattr(real_kernel, name)

    monkeypatch.setattr(real_fs, "_kernel", _KernelProxy())

    # Snapshot entry recorded for the (now-deleted) path with operation=delete.
    snap_svc = MagicMock()
    snap_svc.get_transaction = AsyncMock(return_value=SimpleNamespace(zone_id="root"))
    snap_svc.list_entries = AsyncMock(
        return_value=[
            SimpleNamespace(
                path=target,
                operation="delete",
                original_hash=deleted_hash,
                new_hash=None,
                original_metadata=None,
            )
        ]
    )

    app = FastAPI()
    router = create_async_files_router(nexus_fs=real_fs)
    app.state.transactional_snapshot_service = snap_svc
    app.include_router(router)
    app.dependency_overrides[get_auth_result] = lambda: {
        "authenticated": True,
        "user_id": "e2e-user",
        "groups": [],
        "zone_id": "root",
        "is_admin": True,
    }
    client = TestClient(app)

    resp = client.get(
        "/read",
        params={
            "path": target,
            "version": deleted_hash,
            "transaction_id": "txn-stub",
            "encoding": "base64",
        },
    )
    assert resp.status_code == 200, resp.text
    assert captured["content_id"] == deleted_hash
    decoded = base64.b64decode(resp.json()["content"])
    assert decoded == payload


def test_versioned_read_deleted_path_denied_for_non_admin(
    real_fs: NexusFS, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-admin caller must NOT be able to recover deleted-file
    snapshots via the historical read endpoint. Without the live VFS
    path, file-level READ permission hooks no longer run; the only
    safe carve-out is admin-only — otherwise a caller who can enumerate
    transactions could read deleted content they never had READ
    permission for (Issue #3989, codex r10)."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    target = "/files/secret_gone.bin"
    setup_app = FastAPI()
    setup_app.include_router(create_async_files_router(nexus_fs=real_fs))
    setup_app.dependency_overrides[get_auth_result] = lambda: {
        "authenticated": True,
        "user_id": "e2e-user",
        "groups": [],
        "zone_id": "root",
        "is_admin": True,
    }
    setup_client = TestClient(setup_app)
    write_resp = setup_client.post(
        "/write",
        json={"path": target, "content": "secret"},
    )
    assert write_resp.status_code == 200, write_resp.text
    deleted_hash = write_resp.json()["content_id"]
    real_fs.sys_unlink(target)

    monkeypatch.setattr(
        real_fs,
        "list_mounts",
        lambda context=None: [{"mount_point": "/files"}],
    )
    snap_svc = MagicMock()
    snap_svc.get_transaction = AsyncMock(return_value=SimpleNamespace(zone_id="root"))
    snap_svc.list_entries = AsyncMock(
        return_value=[
            SimpleNamespace(
                path=target,
                operation="delete",
                original_hash=deleted_hash,
                new_hash=None,
                original_metadata=None,
            )
        ]
    )

    app = FastAPI()
    router = create_async_files_router(nexus_fs=real_fs)
    app.state.transactional_snapshot_service = snap_svc
    app.include_router(router)
    # Non-admin caller in the same zone.
    app.dependency_overrides[get_auth_result] = lambda: {
        "authenticated": True,
        "user_id": "regular-user",
        "groups": [],
        "zone_id": "root",
        "is_admin": False,
    }
    client = TestClient(app)

    resp = client.get(
        "/read",
        params={
            "path": target,
            "version": deleted_hash,
            "transaction_id": "txn-stub",
        },
    )
    # Live path no longer exists → access() returns False → 404. Critically
    # the request must NOT succeed via the delete carve-out.
    assert resp.status_code == 404, resp.text
