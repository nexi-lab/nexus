"""Tests for CLI timing infrastructure."""

import time

import pytest

from nexus.cli.timing import CommandTiming, timing_enabled


class TestCommandTiming:
    """Phase-granular timing with context managers."""

    def test_phase_records_duration(self) -> None:
        timing = CommandTiming()
        with timing.phase("test"):
            time.sleep(0.01)

        assert "test" in timing.phases
        assert timing.phases["test"] > 0

    def test_multiple_phases(self) -> None:
        timing = CommandTiming()
        with timing.phase("connect"):
            time.sleep(0.005)
        with timing.phase("server"):
            time.sleep(0.005)

        assert "connect" in timing.phases
        assert "server" in timing.phases
        assert len(timing.phases) == 2

    def test_total_ms_increases(self) -> None:
        timing = CommandTiming()
        t1 = timing.total_ms
        time.sleep(0.01)
        t2 = timing.total_ms
        assert t2 > t1

    def test_to_dict(self) -> None:
        timing = CommandTiming()
        with timing.phase("server"):
            pass

        d = timing.to_dict()
        assert "total_ms" in d
        assert "phases" in d
        assert "server" in d["phases"]

    def test_format_short_with_server(self) -> None:
        timing = CommandTiming()
        with timing.phase("server"):
            pass

        result = timing.format_short()
        assert "ms total" in result
        assert "server:" in result

    def test_format_short_without_server(self) -> None:
        timing = CommandTiming()
        with timing.phase("connect"):
            pass

        result = timing.format_short()
        assert "ms]" in result
        assert "server:" not in result

    def test_format_breakdown(self) -> None:
        timing = CommandTiming()
        with timing.phase("connect"):
            pass
        with timing.phase("server"):
            pass

        result = timing.format_breakdown()
        assert "total:" in result
        assert "connect:" in result
        assert "server:" in result

    def test_phase_exception_still_records(self) -> None:
        """Phase timing should be recorded even when an exception occurs."""
        timing = CommandTiming()
        with pytest.raises(ValueError, match="test error"), timing.phase("failing"):
            raise ValueError("test error")

        assert "failing" in timing.phases
        assert timing.phases["failing"] >= 0


class TestTimingEnabled:
    """Timing display controlled by verbosity and env var."""

    def test_disabled_by_default(self) -> None:
        assert timing_enabled(0) is False

    def test_enabled_by_verbosity(self) -> None:
        assert timing_enabled(1) is True
        assert timing_enabled(2) is True
        assert timing_enabled(3) is True

    def test_enabled_by_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_TIMING", "1")
        assert timing_enabled(0) is True

    def test_disabled_when_env_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEXUS_TIMING", raising=False)
        assert timing_enabled(0) is False
