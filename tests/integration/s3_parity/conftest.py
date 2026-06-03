"""Shared boot + harness for S3-vs-local parity tests (#4267).

One in-process NexusFS kernel (spawns the Rust ``nexusd-cluster``), two mounts:
  - /local/data  -> PathLocalBackend   (control, Rust driver-path-local)
  - /s3/data     -> PathS3Backend        (Rust driver-s3, pointed at MinIO)

Every parity test runs the same op against both mounts and asserts identical
results. The storage backend is the only independent variable.

WHY MinIO (not moto): S3 I/O is served Rust-side, so Python's moto never sees
the kernel's S3 calls. The cluster binary must be built with the S3 driver:

    cargo build -p nexus-cluster --bin nexusd-cluster --features backends/driver-s3

and a real S3 endpoint (MinIO) must be reachable. The suite skips cleanly when
either prerequisite is missing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

pytest.importorskip("boto3", reason="boto3 required for S3 parity tests")

import boto3
from botocore.client import Config

from nexus.backends.storage.path_local import PathLocalBackend
from nexus.backends.storage.path_s3 import PathS3Backend
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.metadata import DT_MOUNT
from nexus.contracts.types import OperationContext
from nexus.core.config import PermissionConfig
from nexus.core.nexus_fs import NexusFS
from nexus.fs._helpers import LOCAL_CONTEXT

# MinIO / S3 endpoint configuration (overridable via env for CI).
S3_ENDPOINT = os.environ.get("NEXUS_TEST_S3_ENDPOINT", "http://localhost:9100")
S3_ACCESS_KEY = os.environ.get("NEXUS_TEST_S3_ACCESS_KEY", "minioadmin")
S3_SECRET_KEY = os.environ.get("NEXUS_TEST_S3_SECRET_KEY", "minioadmin")
S3_REGION = os.environ.get("NEXUS_TEST_S3_REGION", "us-east-1")
S3_BUCKET = os.environ.get("NEXUS_TEST_S3_BUCKET", "nexus-parity-bucket")

LOCAL_MP = "/local/data"
S3_MP = "/s3/data"


@dataclass
class ParityHarness:
    """Two equivalent mount points over one kernel."""

    fs: NexusFS
    local_mp: str
    s3_mp: str
    ctx: OperationContext

    def paths(self, rel: str) -> tuple[str, str]:
        """Return (local_path, s3_path) for a relative file name."""
        return f"{self.local_mp}/{rel}", f"{self.s3_mp}/{rel}"


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        region_name=S3_REGION,
        config=Config(signature_version="s3v4"),
    )


@pytest.fixture(scope="session")
def minio_bucket() -> str:
    """Skip the suite unless MinIO is reachable; ensure the bucket exists."""
    client = _s3_client()
    try:
        client.list_buckets()
    except Exception as exc:  # noqa: BLE001 — any connection failure -> skip
        pytest.skip(
            f"MinIO not reachable at {S3_ENDPOINT} ({type(exc).__name__}). Start one, e.g. "
            f"`docker run -d -p 9100:9000 -e MINIO_ROOT_USER=minioadmin "
            f"-e MINIO_ROOT_PASSWORD=minioadmin minio/minio server /data`, "
            f"or set NEXUS_TEST_S3_ENDPOINT."
        )
    try:
        client.create_bucket(Bucket=S3_BUCKET)
    except client.exceptions.BucketAlreadyOwnedByYou:
        pass
    except Exception as exc:  # noqa: BLE001
        if "BucketAlreadyOwnedByYou" not in str(exc) and "BucketAlreadyExists" not in str(exc):
            raise
    return S3_BUCKET


def _boot_kernel(tmp_path: Path, bucket: str, *, enforce: bool) -> tuple[NexusFS, object]:
    """Boot a NexusFS with both backends mounted. `enforce` toggles ReBAC.

    Returns (kernel, kernel_client) so the caller can close both. Skips if the
    nexus-cluster binary is missing or was built without the S3 driver.
    """
    from nexus.remote.kernel_client import KernelClient

    local_root = tmp_path / "local_store"
    local_root.mkdir(parents=True, exist_ok=True)

    k = KernelClient()
    k.set_metastore_path(str(tmp_path / "metastore.redb"))
    try:
        k.open()
    except FileNotFoundError as exc:
        pytest.skip(
            f"S3 parity tests require the `nexusd-cluster` binary on PATH (KernelClient "
            f"spawns it). Build with `cargo build -p nexus-cluster --bin nexusd-cluster "
            f"--features backends/driver-s3` and symlink it onto PATH. ({exc})"
        )

    kernel = NexusFS(metadata_store=k, permissions=PermissionConfig(enforce=enforce))
    kernel._init_cred = OperationContext(
        user_id="test", groups=[], zone_id=ROOT_ZONE_ID, is_admin=True
    )
    kernel.sys_setattr(
        LOCAL_MP, entry_type=DT_MOUNT, backend=PathLocalBackend(root_path=str(local_root))
    )
    # Unique prefix per test: MinIO persists across tests (unlike a fresh moto mock).
    s3_backend = PathS3Backend(
        bucket_name=bucket,
        prefix=f"parity/{tmp_path.name}",
        endpoint_url=S3_ENDPOINT,
        access_key_id=S3_ACCESS_KEY,
        secret_access_key=S3_SECRET_KEY,
        region_name=S3_REGION,
    )
    try:
        kernel.sys_setattr(S3_MP, entry_type=DT_MOUNT, backend=s3_backend)
    except Exception as exc:  # noqa: BLE001
        if "unknown backend_type" in str(exc):
            kernel.close()
            k.close()
            pytest.skip(
                "nexusd-cluster was built without the S3 driver. Rebuild with "
                "`cargo build -p nexus-cluster --bin nexusd-cluster "
                "--features backends/driver-s3`."
            )
        raise
    return kernel, k


@pytest.fixture
def parity_kernel(tmp_path: Path, minio_bucket: str):
    """ReBAC disabled (enforce=False): for VFS/batch/range parity tests."""
    kernel, k = _boot_kernel(tmp_path, minio_bucket, enforce=False)
    try:
        yield ParityHarness(fs=kernel, local_mp=LOCAL_MP, s3_mp=S3_MP, ctx=LOCAL_CONTEXT)
    finally:
        kernel.close()
        k.close()


@pytest.fixture
def parity_kernel_enforced(tmp_path: Path, minio_bucket: str):
    """ReBAC enabled (enforce=True): for allow/deny parity tests.

    Wires a MemoryPermissionEnforcer-backed ReBACManager (in-memory SQLite)
    into the NexusFS kernel so that ReBAC deny/allow is exercised without a
    full server stack.  The ReBACManager is enlisted as the ``"rebac_mgr"``
    service so tests can call ``.rebac_write()`` to issue grants.
    """
    from sqlalchemy import create_engine

    from nexus.bricks.rebac.checker import PermissionChecker
    from nexus.bricks.rebac.consistency.metastore_namespace_store import (
        MetastoreNamespaceStore,
    )
    from nexus.bricks.rebac.enforcer import PermissionEnforcer
    from nexus.bricks.rebac.manager import ReBACManager
    from nexus.bricks.rebac.permission_hook import RebacPermissionCheckHook
    from nexus.storage.models import Base
    from tests.testkit.metadata import InMemoryNexusFS

    kernel, k = _boot_kernel(tmp_path, minio_bucket, enforce=True)

    # Build an in-memory ReBACManager (same pattern as unit tests).
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    rebac_mgr = ReBACManager(
        engine=engine,
        cache_ttl_seconds=300,
        max_depth=10,
        namespace_store=MetastoreNamespaceStore(InMemoryNexusFS()),
    )

    admin = OperationContext(user_id="admin", groups=[], zone_id=ROOT_ZONE_ID, is_admin=True)
    kernel._init_cred = admin

    # Wire the permission hook into the live kernel.
    enforcer = PermissionEnforcer(rebac_manager=rebac_mgr)
    checker = PermissionChecker(
        permission_enforcer=enforcer,
        metadata_store=k,
        default_context=admin,
        enforce_permissions=True,
    )
    hook = RebacPermissionCheckHook(
        checker=checker,
        metadata_store=k,
        default_context=admin,
        enforce_permissions=True,
        permission_enforcer=enforcer,
        descendant_checker=None,
        lease_table=None,
    )
    kernel.sys_setattr("/__sys__/services/permission", service=hook)

    # Expose rebac_mgr for test-side grant writes.
    kernel._local_services["rebac_mgr"] = rebac_mgr

    try:
        yield ParityHarness(fs=kernel, local_mp=LOCAL_MP, s3_mp=S3_MP, ctx=admin)
    finally:
        kernel.close()
        rebac_mgr.close()
        k.close()
