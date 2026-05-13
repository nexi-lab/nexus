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
