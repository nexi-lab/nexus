"""Exhaustive parametrized tests for TTL bucket routing (Issue #3405).

Tests the ceil_bucket() pure function with every boundary value,
invalid inputs, and edge cases.
"""

from __future__ import annotations

import pytest

from nexus.backends.transports.blob_pack_local_transport import TTL_BUCKETS, ceil_bucket


class TestCeilBucketBoundaries:
    """Test exact boundary values for each bucket."""

    @pytest.mark.parametrize(
        "ttl_seconds, expected_bucket",
        [
            # Bucket 1m: <= 300s (5 minutes)
            (0.001, "1m"),  # Smallest positive TTL
            (1, "1m"),  # 1 second
            (30, "1m"),  # 30 seconds
            (59, "1m"),  # 59 seconds
            (60, "1m"),  # 1 minute exactly
            (120, "1m"),  # 2 minutes
            (299, "1m"),  # Just under 5 min
            (300, "1m"),  # Exactly 5 min (upper bound)
            # Bucket 5m: 301–1800s (5min–30min)
            (301, "5m"),  # Just over 5 min
            (600, "5m"),  # 10 minutes
            (900, "5m"),  # 15 minutes
            (1799, "5m"),  # Just under 30 min
            (1800, "5m"),  # Exactly 30 min
            # Bucket 1h: 1801–14400s (30min–4h)
            (1801, "1h"),  # Just over 30 min
            (3600, "1h"),  # 1 hour
            (7200, "1h"),  # 2 hours
            (14399, "1h"),  # Just under 4 hours
            (14400, "1h"),  # Exactly 4 hours
            # Bucket 1d: 14401–172800s (4h–48h)
            (14401, "1d"),  # Just over 4 hours
            (43200, "1d"),  # 12 hours
            (86400, "1d"),  # 24 hours
            (172799, "1d"),  # Just under 48 hours
            (172800, "1d"),  # Exactly 48 hours
            # Bucket 1w: 172801–1209600s (48h–14 days)
            (172801, "1w"),  # Just over 48 hours
            (604800, "1w"),  # 7 days
            (1209599, "1w"),  # Just under 14 days
            (1209600, "1w"),  # Exactly 14 days
            # Exceeds all buckets → permanent (None)
            (1209601, None),  # Just over 14 days
            (2592000, None),  # 30 days
            (31536000, None),  # 365 days
            (999999999, None),  # Very large TTL
        ],
    )
    def test_bucket_assignment(self, ttl_seconds: float, expected_bucket: str | None) -> None:
        assert ceil_bucket(ttl_seconds) == expected_bucket


class TestCeilBucketFloats:
    """Test float TTL values (sub-second precision)."""

    def test_fractional_seconds(self) -> None:
        assert ceil_bucket(0.5) == "1m"
        assert ceil_bucket(0.001) == "1m"
        assert ceil_bucket(299.999) == "1m"
        assert ceil_bucket(300.001) == "5m"

    def test_large_float(self) -> None:
        assert ceil_bucket(1209600.0) == "1w"
        assert ceil_bucket(1209600.1) is None


class TestCeilBucketInvalidInputs:
    """Test invalid TTL values."""

    def test_zero_ttl(self) -> None:
        with pytest.raises(ValueError, match="TTL must be positive"):
            ceil_bucket(0)

    def test_negative_ttl(self) -> None:
        with pytest.raises(ValueError, match="TTL must be positive"):
            ceil_bucket(-1)

    def test_negative_large(self) -> None:
        with pytest.raises(ValueError, match="TTL must be positive"):
            ceil_bucket(-999999)

    def test_negative_float(self) -> None:
        with pytest.raises(ValueError, match="TTL must be positive"):
            ceil_bucket(-0.001)


class TestTTLBucketsConfig:
    """Test the TTL_BUCKETS configuration itself."""

    def test_buckets_are_sorted_by_max_ttl(self) -> None:
        """Buckets must be sorted by max_ttl for ceil_bucket to work."""
        max_ttls = [max_ttl for _, max_ttl, _ in TTL_BUCKETS]
        assert max_ttls == sorted(max_ttls)

    def test_buckets_have_positive_intervals(self) -> None:
        for name, max_ttl, interval in TTL_BUCKETS:
            assert max_ttl > 0, f"Bucket {name} has non-positive max_ttl"
            assert interval > 0, f"Bucket {name} has non-positive interval"

    def test_rotation_interval_less_than_max_ttl(self) -> None:
        """Rotation interval should be <= max_ttl for reasonable behavior."""
        for name, max_ttl, interval in TTL_BUCKETS:
            assert interval <= max_ttl, (
                f"Bucket {name}: rotation interval ({interval}s) > max_ttl ({max_ttl}s)"
            )

    def test_bucket_count(self) -> None:
        assert len(TTL_BUCKETS) == 5
