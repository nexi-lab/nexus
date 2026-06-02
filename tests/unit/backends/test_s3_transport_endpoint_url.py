"""Unit tests for S3Transport endpoint_url + signature_version (Issue #4266)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nexus.backends.transports.s3_transport import S3Transport


@pytest.fixture
def fake_boto():
    """Patch boto3.Session so S3Transport.__init__ doesn't touch the network."""
    with patch("nexus.backends.transports.s3_transport.boto3") as boto:
        session = MagicMock()
        client = MagicMock()
        session.client.return_value = client
        # Simulate boto3 resolving no region by default (env/profile/config empty).
        # Tests that exercise resolution set this explicitly.
        session.region_name = None
        boto.Session.return_value = session
        yield boto, session, client


def test_endpoint_url_threads_into_session_client(fake_boto):
    _, session, _ = fake_boto
    S3Transport(
        bucket_name="b",
        region_name="us-east-1",
        endpoint_url="http://minio.local:9000",
        access_key_id="ak",
        secret_access_key="sk",
    )
    kwargs = session.client.call_args.kwargs
    assert kwargs["endpoint_url"] == "http://minio.local:9000"


def test_endpoint_url_not_passed_when_unset(fake_boto):
    _, session, _ = fake_boto
    S3Transport(bucket_name="b", region_name="us-east-1")
    kwargs = session.client.call_args.kwargs
    assert "endpoint_url" not in kwargs


def test_region_defaults_to_auto_when_endpoint_set_and_no_region(fake_boto):
    # boto3 resolves no region (session.region_name is None) → fall back to "auto".
    _, session, _ = fake_boto
    session.region_name = None
    t = S3Transport(
        bucket_name="b",
        endpoint_url="https://acct.r2.cloudflarestorage.com",
        access_key_id="ak",
        secret_access_key="sk",
    )
    client_kwargs = session.client.call_args.kwargs
    assert client_kwargs.get("region_name") == "auto"
    assert t.region_name == "auto"


def test_endpoint_uses_boto3_resolved_region_over_auto(fake_boto):
    """endpoint_url, no explicit region, but boto3 resolves one (env/profile/config)
    → that region wins, never 'auto'."""
    _, session, _ = fake_boto
    session.region_name = "us-west-2"  # what boto3's chain resolved
    t = S3Transport(
        bucket_name="b",
        endpoint_url="http://minio.local:9000",
        access_key_id="ak",
        secret_access_key="sk",
    )
    client_kwargs = session.client.call_args.kwargs
    assert client_kwargs.get("region_name") == "us-west-2"
    assert t.region_name == "us-west-2"


def test_explicit_region_preserved_with_endpoint(fake_boto):
    boto, _, _ = fake_boto
    S3Transport(
        bucket_name="b",
        region_name="us-east-1",
        endpoint_url="http://minio.local:9000",
        access_key_id="ak",
        secret_access_key="sk",
    )
    session_kwargs = boto.Session.call_args.kwargs
    assert session_kwargs["region_name"] == "us-east-1"


def test_signature_version_s3v4_by_default(fake_boto):
    _, session, _ = fake_boto
    S3Transport(bucket_name="b", region_name="us-east-1")
    boto_config = session.client.call_args.kwargs["config"]
    assert boto_config.signature_version == "s3v4"


def test_endpoint_url_attribute_exposed(fake_boto):
    t = S3Transport(
        bucket_name="b",
        region_name="us-east-1",
        endpoint_url="http://minio.local:9000",
        access_key_id="ak",
        secret_access_key="sk",
    )
    assert t.endpoint_url == "http://minio.local:9000"


def test_endpoint_url_attribute_none_when_unset(fake_boto):
    t = S3Transport(bucket_name="b", region_name="us-east-1")
    assert t.endpoint_url is None
