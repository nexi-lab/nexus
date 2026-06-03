"""Shared boot + harness for S3-vs-local parity tests (#4267).

One in-process NexusFS kernel, two mounts:
  - /local/data  -> PathLocalBackend   (control)
  - /s3/data     -> PathS3Backend       (moto mock_aws)

Every parity test runs the same op against both mounts and asserts identical
results. The storage backend is the only independent variable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

pytest.importorskip("moto", reason="moto required for S3 parity tests")
pytest.importorskip("boto3", reason="boto3 required for S3 parity tests")

import boto3
from moto import mock_aws

from nexus.backends.storage.path_local import PathLocalBackend
from nexus.backends.storage.path_s3 import PathS3Backend
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.metadata import DT_MOUNT
from nexus.contracts.types import OperationContext
from nexus.core.config import PermissionConfig
from nexus.core.nexus_fs import NexusFS
from nexus.fs._helpers import LOCAL_CONTEXT

BUCKET_NAME = "nexus-parity-bucket"
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


def _boot_kernel(tmp_path: Path, *, enforce: bool) -> tuple[NexusFS, object]:
    """Boot a NexusFS with both backends mounted. `enforce` toggles ReBAC.

    Returns (kernel, kernel_client) so the caller can close the client.
    Raises pytest.skip if nexus-cluster is not available.
    """
    from nexus.remote.kernel_client import KernelClient

    local_root = tmp_path / "local_store"
    local_root.mkdir(parents=True, exist_ok=True)

    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=BUCKET_NAME)

    k = KernelClient()
    k.set_metastore_path(str(tmp_path / "metastore.redb"))
    try:
        k.open()
    except FileNotFoundError as exc:
        pytest.skip(
            f"S3 parity tests require the ``nexus-cluster`` binary on PATH "
            f"(KernelClient spawns it). Build with "
            f"``cargo build -p nexus-profiles-cluster`` and symlink "
            f"``nexusd-cluster`` -> ``nexus-cluster``. ({exc})"
        )

    kernel = NexusFS(
        metadata_store=k,
        permissions=PermissionConfig(enforce=enforce),
    )
    kernel._init_cred = OperationContext(
        user_id="test", groups=[], zone_id=ROOT_ZONE_ID, is_admin=True
    )
    kernel.sys_setattr(
        LOCAL_MP, entry_type=DT_MOUNT, backend=PathLocalBackend(root_path=str(local_root))
    )
    kernel.sys_setattr(
        S3_MP, entry_type=DT_MOUNT, backend=PathS3Backend(bucket_name=BUCKET_NAME, prefix="")
    )
    return kernel, k


@pytest.fixture
def parity_kernel(tmp_path: Path):
    """ReBAC disabled (enforce=False): for VFS/batch/range parity tests."""
    with mock_aws():
        kernel, k = _boot_kernel(tmp_path, enforce=False)
        yield ParityHarness(fs=kernel, local_mp=LOCAL_MP, s3_mp=S3_MP, ctx=LOCAL_CONTEXT)
        kernel.close()
        k.close()
