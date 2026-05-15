"""Tests for S3 archive storage backend (uses moto)."""

import pytest

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

from moto import mock_aws  # noqa: E402

from nexus.bricks.archive.storage.s3 import S3ArchiveStorage  # noqa: E402


@mock_aws
def test_put_then_list(tmp_path):
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="test-bucket")
    storage = S3ArchiveStorage(bucket="test-bucket", prefix="archives/", region="us-east-1")
    src = tmp_path / "a.nexus"
    src.write_bytes(b"data")
    storage.put("daily/a.nexus", src)
    entries = storage.list("daily/")
    assert any(e.key == "daily/a.nexus" for e in entries)


@mock_aws
def test_get_round_trip(tmp_path):
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="test-bucket")
    storage = S3ArchiveStorage(bucket="test-bucket", prefix="", region="us-east-1")
    src = tmp_path / "a.nexus"
    src.write_bytes(b"contents")
    storage.put("a.nexus", src)
    target = tmp_path / "out.nexus"
    storage.get("a.nexus", target)
    assert target.read_bytes() == b"contents"


@mock_aws
def test_delete(tmp_path):
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="test-bucket")
    storage = S3ArchiveStorage(bucket="test-bucket", prefix="", region="us-east-1")
    src = tmp_path / "a.nexus"
    src.write_bytes(b"data")
    storage.put("a.nexus", src)
    storage.delete("a.nexus")
    assert storage.list("") == []
