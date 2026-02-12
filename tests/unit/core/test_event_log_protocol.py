"""Tests for EventLogProtocol type-checking compliance.

Verifies that both WALEventLog and PGEventLog satisfy the
runtime_checkable EventLogProtocol interface.

Issue #1397
"""

from __future__ import annotations

import pytest

from nexus.core.protocols.event_log import EventLogConfig, EventLogProtocol


class TestEventLogProtocol:
    """Verify protocol shape compliance."""

    def test_wal_event_log_satisfies_protocol(self) -> None:
        """WALEventLog must be a structural subtype of EventLogProtocol."""
        try:
            from nexus.core.event_log_wal import WALEventLog
        except ImportError:
            pytest.skip("_nexus_wal extension not available")

        config = EventLogConfig()
        log = WALEventLog(config)
        assert isinstance(log, EventLogProtocol)
        log._wal.close()

    def test_pg_event_log_satisfies_protocol(self) -> None:
        """PGEventLog must be a structural subtype of EventLogProtocol."""
        from unittest.mock import MagicMock

        from nexus.core.event_log_pg import PGEventLog

        config = EventLogConfig()
        log = PGEventLog(config, session_factory=MagicMock())
        assert isinstance(log, EventLogProtocol)

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
