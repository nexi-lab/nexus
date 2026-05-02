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
