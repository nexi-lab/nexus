"""
Tests for Zookie Consistency Tokens (Issue #1187)
"""

import time

import pytest

from nexus.core.zookie import (
    ConsistencyTimeoutError,
    InvalidZookieError,
    Zookie,
    _compute_checksum,
)


class TestZookieEncodeDecode:
    """Tests for Zookie.encode() and Zookie.decode()."""

    def test_encode_decode_roundtrip(self):
        """Basic encode/decode roundtrip should preserve all values."""
        zone_id = "zone_123"
        revision = 42

        token = Zookie.encode(zone_id, revision)
        zookie = Zookie.decode(token)

        assert zookie.zone_id == zone_id
        assert zookie.revision == revision
        assert zookie.created_at_ms > 0

    def test_encode_decode_with_special_chars_in_zone(self):
        """Zone IDs with special characters should be handled correctly."""
        special_zones = [
            "org_123",
            "zone-with-dashes",
            "zone.with.dots",
            "zone/with/slashes",
            "zone:with:colons",
            "zone@with@at",
            "unicode_\u4e2d\u6587",  # Chinese characters
        ]

        for zone_id in special_zones:
            token = Zookie.encode(zone_id, 1)
            zookie = Zookie.decode(token)
            assert zookie.zone_id == zone_id, f"Failed for zone: {zone_id}"

    def test_encode_decode_with_various_revisions(self):
        """Various revision values should be handled correctly."""
        revisions = [0, 1, 100, 999999, 2**31 - 1, 2**63 - 1]

        for revision in revisions:
            token = Zookie.encode("zone", revision)
            zookie = Zookie.decode(token)
            assert zookie.revision == revision, f"Failed for revision: {revision}"

    def test_token_format_is_correct(self):
        """Token should have the expected format: nz1.{zone}.{rev}.{ts}.{checksum}."""
        token = Zookie.encode("test_zone", 123)
        parts = token.split(".")

        assert len(parts) == 5
        assert parts[0] == "nz1"  # Version
        assert len(parts[4]) == 8  # Checksum is 8 hex chars

    def test_created_at_is_recent(self):
        """created_at_ms should be close to current time."""
        before = int(time.time() * 1000)  # Use int to match encode() behavior
        token = Zookie.encode("zone", 1)
        after = int(time.time() * 1000) + 1  # Add 1ms buffer for rounding

        zookie = Zookie.decode(token)
        assert before <= zookie.created_at_ms <= after


class TestZookieDecodeErrors:
    """Tests for Zookie.decode() error handling."""

    def test_decode_empty_token_raises(self):
        """Empty token should raise InvalidZookieError."""
        with pytest.raises(InvalidZookieError) as exc_info:
            Zookie.decode("")
        assert "non-empty string" in str(exc_info.value)

    def test_decode_none_raises(self):
        """None token should raise InvalidZookieError."""
        with pytest.raises(InvalidZookieError) as exc_info:
            Zookie.decode(None)  # type: ignore
        assert "non-empty string" in str(exc_info.value)

    def test_decode_wrong_format_raises(self):
        """Token with wrong number of parts should raise InvalidZookieError."""
        invalid_tokens = [
            "nz1",
            "nz1.zone",
            "nz1.zone.123",
            "nz1.zone.123.ts",
            "nz1.a.b.c.d.e.f",  # Too many parts
        ]

        for token in invalid_tokens:
            with pytest.raises(InvalidZookieError) as exc_info:
                Zookie.decode(token)
            assert "expected 5 parts" in str(exc_info.value)

    def test_decode_wrong_version_raises(self):
        """Token with wrong version should raise InvalidZookieError."""
        # Manually construct a token with wrong version
        token = "nz2.dGVzdA.123.1234567890.abcd1234"
        with pytest.raises(InvalidZookieError) as exc_info:
            Zookie.decode(token)
        assert "Unsupported zookie version" in str(exc_info.value)

    def test_decode_invalid_checksum_raises(self):
        """Token with wrong checksum should raise InvalidZookieError."""
        # Create valid token and corrupt the checksum
        valid_token = Zookie.encode("zone", 123)
        parts = valid_token.split(".")
        parts[4] = "00000000"  # Corrupt checksum
        corrupted_token = ".".join(parts)

        with pytest.raises(InvalidZookieError) as exc_info:
            Zookie.decode(corrupted_token)
        assert "checksum" in str(exc_info.value).lower()

    def test_decode_invalid_revision_raises(self):
        """Token with invalid revision should raise InvalidZookieError."""
        # Create a token manually with invalid revision
        zone_b64 = "dGVzdA"  # "test" base64 encoded
        revision = "not_a_number"
        ts = "1234567890"
        payload = f"nz1.{zone_b64}.{revision}.{ts}"
        checksum = _compute_checksum(payload)
        token = f"{payload}.{checksum}"

        with pytest.raises(InvalidZookieError) as exc_info:
            Zookie.decode(token)
        assert "Invalid revision" in str(exc_info.value)

    def test_decode_negative_revision_raises(self):
        """Token with negative revision should raise InvalidZookieError."""
        zone_b64 = "dGVzdA"
        revision = "-1"
        ts = "1234567890"
        payload = f"nz1.{zone_b64}.{revision}.{ts}"
        checksum = _compute_checksum(payload)
        token = f"{payload}.{checksum}"

        with pytest.raises(InvalidZookieError) as exc_info:
            Zookie.decode(token)
        assert "non-negative" in str(exc_info.value).lower()


class TestZookieChecksumValidation:
    """Tests for zookie checksum tamper detection."""

    def test_checksum_detects_zone_tampering(self):
        """Modifying zone in token should be detected."""
        token = Zookie.encode("original_zone", 100)
        parts = token.split(".")
        # Change zone to different base64
        import base64

        parts[1] = base64.urlsafe_b64encode(b"hacked_zone").decode().rstrip("=")
        tampered_token = ".".join(parts)

        with pytest.raises(InvalidZookieError) as exc_info:
            Zookie.decode(tampered_token)
        assert "checksum" in str(exc_info.value).lower()

    def test_checksum_detects_revision_tampering(self):
        """Modifying revision in token should be detected."""
        token = Zookie.encode("zone", 100)
        parts = token.split(".")
        parts[2] = "999999"  # Change revision
        tampered_token = ".".join(parts)

        with pytest.raises(InvalidZookieError) as exc_info:
            Zookie.decode(tampered_token)
        assert "checksum" in str(exc_info.value).lower()

    def test_checksum_detects_timestamp_tampering(self):
        """Modifying timestamp in token should be detected."""
        token = Zookie.encode("zone", 100)
        parts = token.split(".")
        parts[3] = "0"  # Change timestamp
        tampered_token = ".".join(parts)

        with pytest.raises(InvalidZookieError) as exc_info:
            Zookie.decode(tampered_token)
        assert "checksum" in str(exc_info.value).lower()


class TestZookieMethods:
    """Tests for Zookie utility methods."""

    def test_is_at_least_returns_true_for_equal(self):
        """is_at_least should return True when revision equals min_revision."""
        zookie = Zookie(zone_id="zone", revision=100, created_at_ms=0)
        assert zookie.is_at_least(100) is True

    def test_is_at_least_returns_true_for_greater(self):
        """is_at_least should return True when revision > min_revision."""
        zookie = Zookie(zone_id="zone", revision=100, created_at_ms=0)
        assert zookie.is_at_least(50) is True

    def test_is_at_least_returns_false_for_less(self):
        """is_at_least should return False when revision < min_revision."""
        zookie = Zookie(zone_id="zone", revision=100, created_at_ms=0)
        assert zookie.is_at_least(150) is False

    def test_age_ms_returns_positive_value(self):
        """age_ms should return a positive value for recently created zookie."""
        token = Zookie.encode("zone", 1)
        zookie = Zookie.decode(token)
        age = zookie.age_ms()
        assert age >= 0
        assert age < 1000  # Should be less than 1 second old

    def test_str_representation(self):
        """__str__ should return human-readable format."""
        zookie = Zookie(zone_id="test_zone", revision=42, created_at_ms=time.time() * 1000)
        s = str(zookie)
        assert "test_zone" in s
        assert "42" in s
        assert "Zookie" in s


class TestConsistencyTimeoutError:
    """Tests for ConsistencyTimeoutError exception."""

    def test_exception_attributes(self):
        """Exception should store all provided attributes."""
        error = ConsistencyTimeoutError(
            message="Timeout waiting for revision",
            zone_id="zone_123",
            requested_revision=100,
            current_revision=95,
            timeout_ms=5000,
        )

        assert error.zone_id == "zone_123"
        assert error.requested_revision == 100
        assert error.current_revision == 95
        assert error.timeout_ms == 5000
        assert "Timeout" in str(error)


class TestInvalidZookieError:
    """Tests for InvalidZookieError exception."""

    def test_exception_attributes(self):
        """Exception should store message and token."""
        error = InvalidZookieError("Invalid format", "bad_token")
        assert error.message == "Invalid format"
        assert error.token == "bad_token"
        assert "Invalid format" in str(error)
