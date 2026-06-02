"""Unit tests for PathS3Backend endpoint_url threading (Issue #4266)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def fake_boto_for_path_s3():
    with patch("nexus.backends.transports.s3_transport.boto3") as boto:
        session = MagicMock()
        client = MagicMock()
        client.head_bucket.return_value = {}
        # versioning probe -> not enabled
        client.get_bucket_versioning.return_value = {"Status": "Suspended"}
        session.client.return_value = client
        boto.Session.return_value = session
        yield boto, session, client


def test_path_s3_threads_endpoint_url(fake_boto_for_path_s3):
    from nexus.backends.storage.path_s3 import PathS3Backend

    backend = PathS3Backend(
        bucket_name="b",
        region_name="us-east-1",
        endpoint_url="http://minio.local:9000",
        access_key_id="ak",
        secret_access_key="sk",
    )
    assert backend.endpoint_url == "http://minio.local:9000"
    assert backend._s3_transport.endpoint_url == "http://minio.local:9000"


def test_path_s3_endpoint_url_optional(fake_boto_for_path_s3):
    from nexus.backends.storage.path_s3 import PathS3Backend

    backend = PathS3Backend(
        bucket_name="b",
        region_name="us-east-1",
    )
    assert backend.endpoint_url is None


def test_path_s3_connection_args_contains_endpoint_url():
    from nexus.backends.storage.path_s3 import PathS3Backend

    assert "endpoint_url" in PathS3Backend.CONNECTION_ARGS


def test_path_s3_signature_version_threads_through(fake_boto_for_path_s3):
    """signature_version reaches S3Transport / botocore.Config."""
    from nexus.backends.storage.path_s3 import PathS3Backend

    _boto, session, _client = fake_boto_for_path_s3
    PathS3Backend(
        bucket_name="b",
        region_name="us-east-1",
        endpoint_url="http://minio.local:9000",
        signature_version="s3",
        access_key_id="ak",
        secret_access_key="sk",
    )
    config = session.client.call_args.kwargs["config"]
    assert config.signature_version == "s3"
