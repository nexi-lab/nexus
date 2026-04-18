"""AWS KMS provider contract tests (issue #3803).

Gated behind @pytest.mark.kms. Requires a reachable KMS endpoint (real AWS or
LocalStack) with a CMK accessible via AWS_KMS_KEY_ID.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import pytest

from nexus.bricks.auth.envelope import EncryptionProvider
from nexus.bricks.auth.tests.test_envelope_contract import EnvelopeProviderContract

KMS_ENDPOINT = os.environ.get("AWS_ENDPOINT_URL")
KMS_REGION = os.environ.get("AWS_REGION", "us-east-1")
KMS_KEY_ID = os.environ.get("AWS_KMS_KEY_ID")


def _kms_available() -> bool:
    if not KMS_KEY_ID:
        return False
    try:
        import boto3
    except ImportError:
        return False
    try:
        kwargs: dict = {"region_name": KMS_REGION}
        if KMS_ENDPOINT:
            kwargs["endpoint_url"] = KMS_ENDPOINT
        client = boto3.client("kms", **kwargs)
        client.describe_key(KeyId=KMS_KEY_ID)
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.kms,
    pytest.mark.skipif(
        not _kms_available(),
        reason="AWS KMS not reachable or AWS_KMS_KEY_ID unset",
    ),
]


@pytest.fixture()
def provider_factory() -> Callable[[], EncryptionProvider]:
    import boto3

    from nexus.bricks.auth.envelope_providers.aws_kms import AwsKmsProvider

    def _make() -> EncryptionProvider:
        kwargs: dict = {"region_name": KMS_REGION}
        if KMS_ENDPOINT:
            kwargs["endpoint_url"] = KMS_ENDPOINT
        kms = boto3.client("kms", **kwargs)
        return AwsKmsProvider(kms, key_id=KMS_KEY_ID)

    return _make


class TestAwsKmsContract(EnvelopeProviderContract):
    """Runs the shared EncryptionProvider contract suite against AWS KMS."""
