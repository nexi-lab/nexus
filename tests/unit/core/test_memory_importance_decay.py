"""Tests for Memory Importance Decay (Issue #1030).

Tests the time-based importance decay functionality for memories.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from nexus.services.memory.memory_api import (
    DEFAULT_DECAY_FACTOR,
    DEFAULT_MIN_IMPORTANCE,
    get_effective_importance,
)


class TestGetEffectiveImportance:
    """Test the get_effective_importance function."""

    def test_no_decay_when_just_accessed(self):
        """Test that recently accessed memories don't decay."""
        now = datetime.now(UTC)
        effective = get_effective_importance(
            importance_original=0.8,
            importance_current=0.8,
            last_accessed_at=now,
            created_at=now - timedelta(days=30),
        )
        # Should be approximately 0.8 (no decay for 0 days)
        assert effective == pytest.approx(0.8, abs=0.01)

    def test_decay_after_one_day(self):
        """Test decay after 1 day without access."""
        now = datetime.now(UTC)
        yesterday = now - timedelta(days=1)
        effective = get_effective_importance(
            importance_original=1.0,
            importance_current=1.0,
            last_accessed_at=yesterday,
            created_at=yesterday - timedelta(days=10),
        )
        # Should be 1.0 * 0.95^1 = 0.95
        assert effective == pytest.approx(0.95, abs=0.01)

    def test_decay_after_ten_days(self):
        """Test decay after 10 days without access."""
        now = datetime.now(UTC)
        ten_days_ago = now - timedelta(days=10)
        effective = get_effective_importance(
            importance_original=0.8,
            importance_current=0.8,
            last_accessed_at=ten_days_ago,
            created_at=ten_days_ago - timedelta(days=10),
        )
        # Should be 0.8 * 0.95^10 ≈ 0.478
        expected = 0.8 * (0.95**10)
        assert effective == pytest.approx(expected, abs=0.01)

    def test_min_importance_floor(self):
        """Test that importance doesn't go below minimum."""
        now = datetime.now(UTC)
        long_ago = now - timedelta(days=365)  # 1 year ago
        effective = get_effective_importance(
            importance_original=0.5,
            importance_current=0.5,
            last_accessed_at=long_ago,
            created_at=long_ago,
        )
        # After 365 days: 0.5 * 0.95^365 ≈ 0.00001
        # Should be clamped to min_importance (0.1)
        assert effective == pytest.approx(DEFAULT_MIN_IMPORTANCE, abs=0.01)

    def test_custom_decay_factor(self):
        """Test with custom decay factor."""
        now = datetime.now(UTC)
        five_days_ago = now - timedelta(days=5)
        effective = get_effective_importance(
            importance_original=1.0,
            importance_current=1.0,
            last_accessed_at=five_days_ago,
            created_at=five_days_ago,
            decay_factor=0.9,  # 10% decay per day
        )
        # Should be 1.0 * 0.9^5 ≈ 0.59
        expected = 1.0 * (0.9**5)
        assert effective == pytest.approx(expected, abs=0.01)

    def test_custom_min_importance(self):
        """Test with custom minimum importance."""
        now = datetime.now(UTC)
        long_ago = now - timedelta(days=365)
        effective = get_effective_importance(
            importance_original=0.5,
            importance_current=0.5,
            last_accessed_at=long_ago,
            created_at=long_ago,
            min_importance=0.2,
        )
        assert effective == pytest.approx(0.2, abs=0.01)

    def test_uses_created_at_when_never_accessed(self):
        """Test fallback to created_at when never accessed."""
        now = datetime.now(UTC)
        three_days_ago = now - timedelta(days=3)
        effective = get_effective_importance(
            importance_original=1.0,
            importance_current=1.0,
            last_accessed_at=None,  # Never accessed
            created_at=three_days_ago,
        )
        # Should decay based on days since creation
        expected = 1.0 * (DEFAULT_DECAY_FACTOR**3)
        assert effective == pytest.approx(expected, abs=0.01)

    def test_uses_original_importance_when_available(self):
        """Test that original importance is preferred over current."""
        now = datetime.now(UTC)
        one_day_ago = now - timedelta(days=1)
        effective = get_effective_importance(
            importance_original=0.9,  # Original
            importance_current=0.5,  # Already decayed
            last_accessed_at=one_day_ago,
            created_at=now - timedelta(days=10),
        )
        # Should use original (0.9), not current (0.5)
        expected = 0.9 * DEFAULT_DECAY_FACTOR
        assert effective == pytest.approx(expected, abs=0.01)

    def test_default_importance_when_none(self):
        """Test default importance (0.5) when both are None."""
        now = datetime.now(UTC)
        effective = get_effective_importance(
            importance_original=None,
            importance_current=None,
            last_accessed_at=now,
            created_at=now,
        )
        # Should use default 0.5
        assert effective == pytest.approx(0.5, abs=0.01)

    def test_handles_naive_datetime(self):
        """Test handling of timezone-naive datetimes."""
        # Create naive datetime (no tzinfo)
        naive_dt = datetime.now() - timedelta(days=1)
        effective = get_effective_importance(
            importance_original=1.0,
            importance_current=1.0,
            last_accessed_at=naive_dt,
            created_at=naive_dt - timedelta(days=5),
        )
        # Should handle gracefully
        assert 0.0 < effective <= 1.0


class TestDecayConstants:
    """Test decay configuration constants."""

    def test_default_decay_factor(self):
        """Test default decay factor is reasonable."""
        assert DEFAULT_DECAY_FACTOR == 0.95
        # 5% decay per day is reasonable

    def test_default_min_importance(self):
        """Test default minimum importance is reasonable."""
        assert DEFAULT_MIN_IMPORTANCE == 0.1
        # 10% floor prevents memories from being completely forgotten


class TestMemoryModelFields:
    """Test that MemoryModel has required fields."""

    def test_memory_model_has_tracking_fields(self):
        """Test MemoryModel has the new tracking fields."""
        from nexus.storage.models import MemoryModel

        # Check field existence
        assert hasattr(MemoryModel, "importance_original")
        assert hasattr(MemoryModel, "last_accessed_at")
        assert hasattr(MemoryModel, "access_count")

    def test_access_count_default(self):
        """Test access_count has default value configured."""
        from nexus.storage.models import MemoryModel

        # Check that the column has a default configured
        # (SQLAlchemy defaults apply on INSERT, not instantiation)
        col = MemoryModel.__table__.c.access_count
        assert col.default is not None or col.server_default is not None


class TestDecayIntegration:
    """Integration tests for decay with Memory API."""

    @pytest.fixture
    def mock_session(self):
        """Create mock database session."""
        session = MagicMock()
        session.commit = MagicMock()
        session.rollback = MagicMock()
        return session

    @pytest.fixture
    def mock_backend(self):
        """Create mock storage backend."""
        backend = MagicMock()
        return backend

    def test_track_memory_access_updates_fields(self, mock_session, mock_backend):
        """Test that accessing a memory updates tracking fields."""
        from nexus.services.memory.memory_api import Memory

        # Create mock memory
        mock_memory = MagicMock()
        mock_memory.access_count = 0
        mock_memory.last_accessed_at = None
        mock_memory.importance = 0.8
        mock_memory.importance_original = None

        # Create Memory API instance
        with patch.object(Memory, "__init__", lambda self, *args, **kwargs: None):
            memory_api = Memory.__new__(Memory)
            memory_api.session = mock_session

            # Call tracking method
            memory_api._track_memory_access(mock_memory)

        # Verify updates
        assert mock_memory.access_count == 1
        assert mock_memory.last_accessed_at is not None
        assert mock_memory.importance_original == 0.8
        mock_session.commit.assert_called_once()

    def test_track_memory_access_increments_count(self, mock_session, mock_backend):
        """Test that access count increments."""
        from nexus.services.memory.memory_api import Memory

        mock_memory = MagicMock()
        mock_memory.access_count = 5
        mock_memory.last_accessed_at = datetime.now(UTC)
        mock_memory.importance = 0.8
        mock_memory.importance_original = 0.8

        with patch.object(Memory, "__init__", lambda self, *args, **kwargs: None):
            memory_api = Memory.__new__(Memory)
            memory_api.session = mock_session
            memory_api._track_memory_access(mock_memory)

        assert mock_memory.access_count == 6

    def test_track_memory_access_preserves_original(self, mock_session, mock_backend):
        """Test that original importance is preserved on subsequent accesses."""
        from nexus.services.memory.memory_api import Memory

        mock_memory = MagicMock()
        mock_memory.access_count = 5
        mock_memory.importance = 0.6  # Decayed
        mock_memory.importance_original = 0.9  # Already set

        with patch.object(Memory, "__init__", lambda self, *args, **kwargs: None):
            memory_api = Memory.__new__(Memory)
            memory_api.session = mock_session
            memory_api._track_memory_access(mock_memory)

        # Should not overwrite existing original
        assert mock_memory.importance_original == 0.9


class TestBatchDecay:
    """Test batch decay functionality."""

    def test_batch_decay_formula_correctness(self):
        """Test that decay formula is mathematically correct."""
        # Test exponential decay: I(t) = I_0 * r^t
        importance = 1.0
        decay_factor = 0.95
        days = 30

        # After 30 days at 5% daily decay
        expected = importance * (decay_factor**days)
        actual = get_effective_importance(
            importance_original=importance,
            importance_current=importance,
            last_accessed_at=datetime.now(UTC) - timedelta(days=days),
            created_at=datetime.now(UTC) - timedelta(days=days + 10),
        )

        assert actual == pytest.approx(expected, abs=0.01)

    def test_half_life_calculation(self):
        """Test decay half-life is reasonable."""
        # Half-life formula: t_1/2 = ln(0.5) / ln(decay_factor)
        import math

        half_life = math.log(0.5) / math.log(DEFAULT_DECAY_FACTOR)
        # With 0.95 decay factor, half-life is about 13.5 days
        assert half_life == pytest.approx(13.5, abs=0.5)
