"""Pure resolution-logic tests for S3Transport (Issue #4266).

These exercise the endpoint/region precedence — the area an adversarial review
loop repeatedly found bugs in — WITHOUT constructing a boto3 client, so they run
in the default unit-test environment where the optional `s3` extra (boto3/botocore)
is not installed. The boto3-client wiring itself is covered by
test_s3_transport_endpoint_url.py (skipped when botocore is absent).
"""

from __future__ import annotations

import pytest

# Importing the module is safe without boto3 (it degrades to boto3=None); the
# helpers under test are pure staticmethods that never touch boto3.
from nexus.backends.transports.s3_transport import S3Transport


@pytest.fixture(autouse=True)
def _clean_aws_env(monkeypatch):
    for var in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3"):
        monkeypatch.delenv(var, raising=False)


# --- _resolve_effective_endpoint -------------------------------------------------


def test_effective_endpoint_explicit_arg_wins(monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://env:9000")
    assert S3Transport._resolve_effective_endpoint("http://arg:9000") == "http://arg:9000"


def test_effective_endpoint_s3_specific_env_beats_global(monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "http://s3-specific:9000")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://global:9000")
    assert S3Transport._resolve_effective_endpoint(None) == "http://s3-specific:9000"


def test_effective_endpoint_global_env_fallback(monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://global:9000")
    assert S3Transport._resolve_effective_endpoint(None) == "http://global:9000"


def test_effective_endpoint_none_when_unset():
    assert S3Transport._resolve_effective_endpoint(None) is None


# --- _resolve_region -------------------------------------------------------------


def test_region_explicit_wins(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    assert S3Transport._resolve_region("us-east-1", "eu-west-1", "http://ep") == "us-east-1"


def test_region_aws_region_env_beats_session_and_auto(monkeypatch):
    # botocore's Session.region_name does NOT reflect AWS_REGION, so it's checked here.
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    assert S3Transport._resolve_region(None, None, "http://ep") == "ap-south-1"


def test_region_aws_region_beats_session(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    assert S3Transport._resolve_region(None, "eu-west-1", None) == "ap-south-1"


def test_region_session_used_when_no_explicit_or_aws_region():
    # session_region carries AWS_DEFAULT_REGION / AWS_PROFILE / ~/.aws/config.
    assert S3Transport._resolve_region(None, "eu-west-1", "http://ep") == "eu-west-1"


def test_region_auto_only_for_custom_endpoint_when_nothing_resolves():
    assert S3Transport._resolve_region(None, None, "http://ep") == "auto"


def test_region_none_without_endpoint_and_nothing_resolves():
    assert S3Transport._resolve_region(None, None, None) is None
