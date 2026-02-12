"""Unit tests for stream token signing and verification (HMAC-SHA256).

Tests cover:
- _sign_stream_token: token format, expiry embedding
- _verify_stream_token: valid tokens, expired tokens, tampered tokens, bad formats
- Constant-time comparison via hmac.compare_digest
- _reset_stream_secret: test isolation
- NEXUS_STREAM_SECRET env var override
- _get_stream_secret: lazy initialization, caching
"""

from __future__ import annotations

import hmac
import time
from unittest.mock import patch

import pytest

from nexus.server.streaming import (
    _get_stream_secret,
    _reset_stream_secret,
    _sign_stream_token,
    _verify_stream_token,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_stream_secret():
    """Reset the module-level secret before and after every test."""
    _reset_stream_secret()
    yield
    _reset_stream_secret()


# ===========================================================================
# _get_stream_secret
# ===========================================================================


class TestGetStreamSecret:
    """Tests for _get_stream_secret initialization."""

    def test_generates_random_secret(self):
        """Without env var, a random 32-byte secret should be generated."""
        secret = _get_stream_secret()
        assert isinstance(secret, bytes)
        assert len(secret) == 32

    def test_returns_same_secret_on_repeated_calls(self):
        """Secret should be cached after first generation."""
        first = _get_stream_secret()
        second = _get_stream_secret()
        assert first is second

    def test_env_var_override(self, monkeypatch):
        """NEXUS_STREAM_SECRET env var should override random generation."""
        monkeypatch.setenv("NEXUS_STREAM_SECRET", "my-custom-secret")
        secret = _get_stream_secret()
        assert secret == b"my-custom-secret"

    def test_reset_then_regenerate(self):
        """After reset, a new secret should be generated."""
        first = _get_stream_secret()
        _reset_stream_secret()
        second = _get_stream_secret()
        # Technically could be equal by coincidence, but 32 random bytes makes this
        # astronomically unlikely.
        assert first != second


# ===========================================================================
# _sign_stream_token
# ===========================================================================


class TestSignStreamToken:
    """Tests for _sign_stream_token."""

    def test_token_format(self):
        """Token should be '{expires_at}.{signature}'."""
        token = _sign_stream_token("/files/test.txt", expires_in=300)

        parts = token.split(".")
        assert len(parts) == 2

        expires_at_str, signature = parts
        assert expires_at_str.isdigit()
        assert len(signature) == 16  # hex[:16]

    def test_expiry_is_in_future(self):
        """expires_at should be current time + expires_in."""
        before = int(time.time())
        token = _sign_stream_token("/test", expires_in=600)
        after = int(time.time())

        expires_at = int(token.split(".")[0])
        assert before + 600 <= expires_at <= after + 600

    def test_different_paths_produce_different_tokens(self):
        """Different paths should produce different signatures."""
        token_a = _sign_stream_token("/a.txt", expires_in=300)
        token_b = _sign_stream_token("/b.txt", expires_in=300)

        sig_a = token_a.split(".")[1]
        sig_b = token_b.split(".")[1]
        assert sig_a != sig_b

    def test_different_zones_produce_different_tokens(self):
        """Different zone_ids should produce different signatures."""
        token_a = _sign_stream_token("/same.txt", expires_in=300, zone_id="zone-a")
        token_b = _sign_stream_token("/same.txt", expires_in=300, zone_id="zone-b")

        sig_a = token_a.split(".")[1]
        sig_b = token_b.split(".")[1]
        assert sig_a != sig_b

    def test_default_zone_id(self):
        """Default zone_id should be 'default'."""
        # Two calls with explicit "default" and implicit default should match
        # (if signed at the same second with the same expires_in).
        with patch("nexus.server.streaming.time") as mock_time:
            mock_time.time.return_value = 1000000.0
            token_explicit = _sign_stream_token("/f.txt", expires_in=60, zone_id="default")
            token_implicit = _sign_stream_token("/f.txt", expires_in=60)
        assert token_explicit == token_implicit


# ===========================================================================
# _verify_stream_token
# ===========================================================================


class TestVerifyStreamToken:
    """Tests for _verify_stream_token."""

    def test_valid_token_is_accepted(self):
        """A freshly-signed token should verify successfully."""
        token = _sign_stream_token("/test.txt", expires_in=300)
        assert _verify_stream_token(token, "/test.txt") is True

    def test_valid_token_with_custom_zone(self):
        """Token should verify when zone matches."""
        token = _sign_stream_token("/test.txt", expires_in=300, zone_id="z1")
        assert _verify_stream_token(token, "/test.txt", zone_id="z1") is True

    def test_wrong_path_rejected(self):
        """Token signed for path A should not verify for path B."""
        token = _sign_stream_token("/a.txt", expires_in=300)
        assert _verify_stream_token(token, "/b.txt") is False

    def test_wrong_zone_rejected(self):
        """Token signed for zone A should not verify for zone B."""
        token = _sign_stream_token("/test.txt", expires_in=300, zone_id="zone-a")
        assert _verify_stream_token(token, "/test.txt", zone_id="zone-b") is False

    def test_expired_token_rejected(self):
        """An expired token should be rejected."""
        # Sign with 0-second TTL, then advance time
        token = _sign_stream_token("/test.txt", expires_in=0)
        # The token's expires_at is int(time.time()) + 0, which is essentially now.
        # Wait or mock time to ensure it's past.
        with patch("nexus.server.streaming.time") as mock_time:
            mock_time.time.return_value = time.time() + 100
            assert _verify_stream_token(token, "/test.txt") is False

    def test_tampered_signature_rejected(self):
        """Modified signature should be rejected."""
        token = _sign_stream_token("/test.txt", expires_in=300)
        expires_at, _sig = token.split(".")
        tampered = f"{expires_at}.{'f' * 16}"
        assert _verify_stream_token(tampered, "/test.txt") is False

    def test_tampered_expiry_rejected(self):
        """Modified expiry should be rejected (signature won't match)."""
        token = _sign_stream_token("/test.txt", expires_in=300)
        _, sig = token.split(".")
        far_future = str(int(time.time()) + 999999)
        tampered = f"{far_future}.{sig}"
        assert _verify_stream_token(tampered, "/test.txt") is False

    def test_malformed_token_no_dot(self):
        """Token without a dot separator should return False."""
        assert _verify_stream_token("nodot", "/test.txt") is False

    def test_malformed_token_too_many_dots(self):
        """Token with more than one dot should return False."""
        assert _verify_stream_token("a.b.c", "/test.txt") is False

    def test_malformed_token_non_numeric_expiry(self):
        """Non-numeric expiry should return False (ValueError caught)."""
        assert _verify_stream_token("notanumber.abcdef1234567890", "/test.txt") is False

    def test_empty_token(self):
        """Empty token string should return False."""
        assert _verify_stream_token("", "/test.txt") is False

    def test_constant_time_comparison_used(self):
        """Verification should use hmac.compare_digest for timing-safe comparison."""
        token = _sign_stream_token("/test.txt", expires_in=300)

        with patch(
            "nexus.server.streaming.hmac.compare_digest", wraps=hmac.compare_digest
        ) as mock_cmp:
            _verify_stream_token(token, "/test.txt")
            mock_cmp.assert_called_once()


# ===========================================================================
# _reset_stream_secret
# ===========================================================================


class TestResetStreamSecret:
    """Tests for _reset_stream_secret."""

    def test_reset_invalidates_old_tokens(self):
        """Tokens signed before reset should not verify after reset."""
        token = _sign_stream_token("/test.txt", expires_in=3600)
        assert _verify_stream_token(token, "/test.txt") is True

        _reset_stream_secret()

        assert _verify_stream_token(token, "/test.txt") is False

    def test_reset_allows_fresh_tokens(self):
        """New tokens signed after reset should verify."""
        _reset_stream_secret()
        token = _sign_stream_token("/test.txt", expires_in=3600)
        assert _verify_stream_token(token, "/test.txt") is True
