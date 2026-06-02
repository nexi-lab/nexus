"""Regression test: nexus_fs_metadata extracts S3 params from PathS3Backend (Issue #4266).

cas_s3 is deferred per the #4260 decision (connector path is primary), so only the
PathS3Backend extraction is exercised here. If cas_s3 is later revived, add a
CASS3Backend case mirroring this one.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("botocore", reason="botocore not installed (s3 extra)")


@pytest.fixture
def fake_boto():
    with patch("nexus.backends.transports.s3_transport.boto3") as boto:
        session = MagicMock()
        client = MagicMock()
        client.head_bucket.return_value = {}
        client.get_bucket_versioning.return_value = {"Status": "Suspended"}
        session.client.return_value = client
        boto.Session.return_value = session
        yield boto, session, client


def test_extract_rust_backend_params_for_path_s3(fake_boto):
    from nexus.backends.storage.path_s3 import PathS3Backend
    from nexus.core.nexus_fs_metadata import MetadataMixin

    backend = PathS3Backend(
        bucket_name="nexus-files",
        region_name="us-east-1",
        endpoint_url="https://acct.r2.cloudflarestorage.com",
        access_key_id="ak",
        secret_access_key="sk",
        prefix="p",
    )
    params = MetadataMixin._extract_rust_backend_params(backend, type(backend).__name__)
    assert params is not None
    assert params["backend_type"] == "s3"
    assert params["s3_bucket"] == "nexus-files"
    assert params["s3_prefix"] == "p"
    assert params["aws_region"] == "us-east-1"
    assert params["aws_access_key"] == "ak"
    assert params["aws_secret_key"] == "sk"
    assert params["s3_endpoint"] == "https://acct.r2.cloudflarestorage.com"
