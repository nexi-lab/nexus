"""Tests for AwsProviderAdapter — pure JSON → MaterializedCredential decoding (#3818)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from nexus.bricks.auth.consumer_providers.aws import AwsProviderAdapter


def test_materialize_extracts_session_token_and_metadata():
    payload = json.dumps(
        {
            "access_key_id": "ASIA1234",
            "secret_access_key": "wJalrXUtnFEMI",
            "session_token": "FwoGZXIvYXdz...",
            "expiration": "2026-04-23T18:00:00+00:00",
            "region": "us-west-2",
            "account_id": "123456789012",
        }
    ).encode()

    out = AwsProviderAdapter().materialize(payload)

    assert out.provider == "aws"
    assert out.access_token == "FwoGZXIvYXdz..."
    assert out.expires_at == datetime(2026, 4, 23, 18, 0, 0, tzinfo=UTC)
    assert out.metadata == {
        "access_key_id": "ASIA1234",
        "secret_access_key": "wJalrXUtnFEMI",
        "region": "us-west-2",
        "account_id": "123456789012",
    }


def test_materialize_handles_missing_optional_fields():
    payload = json.dumps(
        {
            "access_key_id": "ASIA1234",
            "secret_access_key": "wJalrXUtnFEMI",
            "session_token": "tok",
            "expiration": "2026-04-23T18:00:00+00:00",
            "region": "us-east-1",
        }
    ).encode()
    out = AwsProviderAdapter().materialize(payload)
    assert "account_id" not in out.metadata


def test_materialize_rejects_malformed_json():
    with pytest.raises(ValueError):
        AwsProviderAdapter().materialize(b"not json")


def test_materialize_rejects_missing_required_field():
    payload = json.dumps({"access_key_id": "x"}).encode()
    with pytest.raises(KeyError):
        AwsProviderAdapter().materialize(payload)


def test_repr_masks_access_token():
    payload = json.dumps(
        {
            "access_key_id": "ASIA1234",
            "secret_access_key": "wJalrXUtnFEMI",
            "session_token": "supersecret",
            "expiration": "2026-04-23T18:00:00+00:00",
            "region": "us-east-1",
        }
    ).encode()
    out = AwsProviderAdapter().materialize(payload)
    assert "supersecret" not in repr(out)
    assert "***" in repr(out)
