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


def test_path_s3_endpoint_url_optional(fake_boto_for_path_s3, monkeypatch):
    from nexus.backends.storage.path_s3 import PathS3Backend

    # No explicit endpoint and no ambient endpoint env → None.
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("AWS_ENDPOINT_URL_S3", raising=False)
    backend = PathS3Backend(
        bucket_name="b",
        region_name="us-east-1",
    )
    assert backend.endpoint_url is None


def test_path_s3_connection_args_contains_endpoint_url():
    from nexus.backends.storage.path_s3 import PathS3Backend

    assert "endpoint_url" in PathS3Backend.CONNECTION_ARGS


def test_path_s3_mirrors_env_resolved_endpoint(fake_boto_for_path_s3, monkeypatch):
    """AWS_ENDPOINT_URL env (no constructor arg) → backend.endpoint_url must mirror
    the transport's effective endpoint, not the raw (None) constructor arg."""
    monkeypatch.setenv("AWS_ENDPOINT_URL", "https://acct.r2.cloudflarestorage.com")
    monkeypatch.delenv("AWS_ENDPOINT_URL_S3", raising=False)
    from nexus.backends.storage.path_s3 import PathS3Backend

    backend = PathS3Backend(
        bucket_name="b", region_name="us-east-1", access_key_id="ak", secret_access_key="sk"
    )
    assert backend._s3_transport.endpoint_url == "https://acct.r2.cloudflarestorage.com"
    assert backend.endpoint_url == "https://acct.r2.cloudflarestorage.com"


def test_path_s3_old_positional_constructor_preserved(fake_boto_for_path_s3, monkeypatch):
    """Back-compat: new params must not shift existing positional slots. Old call:
    PathS3Backend(bucket, region, credentials_path, prefix, access_key, secret_key)."""
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("AWS_ENDPOINT_URL_S3", raising=False)
    from nexus.backends.storage.path_s3 import PathS3Backend

    backend = PathS3Backend("b", "us-east-1", None, "myprefix", "AKIA", "secret")
    assert backend.prefix == "myprefix"  # 4th positional stays prefix
    assert backend._access_key_id == "AKIA"  # 5th positional stays access_key_id
    assert backend._secret_access_key == "secret"  # 6th positional stays secret
    assert backend.endpoint_url is None  # new param not bound positionally


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
