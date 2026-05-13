from typing import Any, cast

import pytest

from nexus.runtime.zone_runner import ZoneRegistry


def test_runner_for_reuses_runner_per_zone() -> None:
    registry = ZoneRegistry()
    try:
        first = registry.runner_for("zone-a")
        second = registry.runner_for("zone-a")
        other = registry.runner_for("zone-b")
        assert first is second
        assert first is not other
    finally:
        registry.stop_all()


def test_all_returns_created_runners() -> None:
    registry = ZoneRegistry()
    try:
        runner_a = registry.runner_for("zone-a")
        runner_b = registry.runner_for("zone-b")
        assert registry.all() == (runner_a, runner_b)
    finally:
        registry.stop_all()


def test_stop_all_stops_every_runner_and_is_idempotent() -> None:
    registry = ZoneRegistry()
    runner_a = registry.runner_for("zone-a")
    runner_b = registry.runner_for("zone-b")
    runner_a.start()
    runner_b.start()

    registry.stop_all()
    registry.stop_all()

    assert not runner_a.is_alive
    assert not runner_b.is_alive


async def _zone_work() -> str:
    return "ok"


def test_stop_all_clears_stopped_runners_for_reuse() -> None:
    registry = ZoneRegistry()
    old_runner = registry.runner_for("zone-a")
    old_runner.start()

    registry.stop_all()
    new_runner = registry.runner_for("zone-a")
    try:
        assert new_runner is not old_runner
        assert new_runner.call_sync(_zone_work) == "ok"
    finally:
        registry.stop_all()


def test_stop_all_keeps_runner_when_stop_raises() -> None:
    class FailingRunner:
        @property
        def is_alive(self) -> bool:
            return True

        def stop(self) -> None:
            raise RuntimeError("stop failed")

    registry = ZoneRegistry()
    runner = FailingRunner()
    cast(dict[str, Any], registry._runners)["zone-a"] = runner

    with pytest.raises(ExceptionGroup) as exc_info:
        registry.stop_all()

    assert "Failed to stop zone runners" in str(exc_info.value)
    assert registry.runner_for("zone-a") is runner


def test_stop_all_keeps_runner_when_stop_returns_but_runner_is_alive() -> None:
    class StillAliveRunner:
        stopped = False

        @property
        def is_alive(self) -> bool:
            return True

        def stop(self) -> None:
            self.stopped = True

    registry = ZoneRegistry()
    runner = StillAliveRunner()
    cast(dict[str, Any], registry._runners)["zone-a"] = runner

    registry.stop_all()

    assert runner.stopped is True
    assert registry.runner_for("zone-a") is runner
