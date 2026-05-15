"""Tests for GithubProviderAdapter — pure JSON → MaterializedCredential decoding (#3818)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from nexus.bricks.auth.consumer_providers.github import GithubProviderAdapter


def test_materialize_classic_pat_no_expiry():
    payload = json.dumps({"token": "ghp_classic", "scopes": ["repo", "read:user"]}).encode()
    out = GithubProviderAdapter().materialize(payload)
    assert out.provider == "github"
    assert out.access_token == "ghp_classic"
    assert out.expires_at is None
    assert out.metadata == {"scopes_csv": "repo,read:user", "token_type": "classic"}


def test_materialize_fine_grained_with_expiry():
    payload = json.dumps(
        {
            "token": "github_pat_xyz",
            "scopes": [],
            "expires_at": "2026-07-01T00:00:00+00:00",
            "token_type": "fine_grained",
        }
    ).encode()
    out = GithubProviderAdapter().materialize(payload)
    assert out.access_token == "github_pat_xyz"
    assert out.expires_at == datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC)
    assert out.metadata["token_type"] == "fine_grained"
    assert out.metadata["scopes_csv"] == ""


def test_materialize_rejects_missing_token():
    # JSON without "token" falls through to raw-bytes parse; the JSON object
    # itself starts with "{" so prefix check fails → ValueError.
    with pytest.raises(ValueError):
        GithubProviderAdapter().materialize(json.dumps({"scopes": []}).encode())


def test_materialize_rejects_malformed_json():
    with pytest.raises(ValueError):
        GithubProviderAdapter().materialize(b"<html>not json</html>")


def test_materialize_accepts_raw_classic_pat():
    """Daemon's `gh auth token` stdout is raw bytes, not JSON."""
    out = GithubProviderAdapter().materialize(b"ghp_rawclassictoken123")
    assert out.access_token == "ghp_rawclassictoken123"
    assert out.metadata == {"scopes_csv": "", "token_type": "classic"}
    assert out.expires_at is None


def test_materialize_accepts_raw_fine_grained_pat():
    out = GithubProviderAdapter().materialize(b"github_pat_finegrained_abc")
    assert out.access_token == "github_pat_finegrained_abc"


def test_materialize_strips_trailing_whitespace_in_raw_token():
    """`gh auth token` output usually ends with a newline."""
    out = GithubProviderAdapter().materialize(b"ghp_withnewline\n")
    assert out.access_token == "ghp_withnewline"


def test_materialize_rejects_unknown_prefix_raw_bytes():
    """Don't accept arbitrary plaintext — must look like a GitHub token."""
    with pytest.raises(ValueError):
        GithubProviderAdapter().materialize(b"random-secret-not-a-pat")


def test_repr_masks_token():
    payload = json.dumps({"token": "ghp_supersecret", "scopes": []}).encode()
    out = GithubProviderAdapter().materialize(payload)
    assert "ghp_supersecret" not in repr(out)


def test_repr_masks_raw_token():
    out = GithubProviderAdapter().materialize(b"ghp_rawsecret_xyz")
    assert "ghp_rawsecret_xyz" not in repr(out)
