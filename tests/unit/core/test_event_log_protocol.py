"""Tests for EventLogProtocol type-checking compliance.

Verifies that WALEventLog satisfies the runtime_checkable
EventLogProtocol interface.

PGEventLog was removed in Issue #1241 — event delivery from
operation_log is now handled by EventDeliveryWorker (outbox).

Issue #1397
"""

from __future__ import annotations

import pytest

from nexus.services.event_log import EventLogConfig, EventLogProtocol


class TestEventLogProtocol:
    """Verify protocol shape compliance."""

    def test_wal_event_log_satisfies_protocol(self) -> None:
        """WALEventLog must be a structural subtype of EventLogProtocol."""
        from nexus.services.event_log.wal_backend import WALEventLog, is_available

        if not is_available():
            pytest.skip("_nexus_wal extension not available")

        config = EventLogConfig()
        log = WALEventLog(config)
        assert isinstance(log, EventLogProtocol)
        log._wal.close()

    def test_event_log_config_defaults(self) -> None:
        """EventLogConfig should have sensible defaults."""
        config = EventLogConfig()
        assert config.segment_size_bytes == 4 * 1024 * 1024
        assert config.sync_mode == "every"
        assert str(config.wal_dir).endswith("wal")

    def test_event_log_config_frozen(self) -> None:
        """EventLogConfig should be immutable."""
        config = EventLogConfig()
        with pytest.raises(AttributeError):
            config.sync_mode = "none"  # type: ignore[misc]
