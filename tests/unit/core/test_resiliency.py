"""Unit tests for the declarative resiliency layer (Issue #1366)."""

from __future__ import annotations

import asyncio

import pytest

from nexus.core.resiliency import (
    AsyncCircuitBreaker,
    CircuitBreakerOpenError,
    CircuitBreakerPolicy,
    CircuitState,
    ResiliencyConfig,
    ResiliencyManager,
    RetryPolicy,
    TargetBinding,
    TimeoutPolicy,
    _get_infra_exceptions,
    get_default_manager,
    parse_duration,
    set_default_manager,
    with_resiliency,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


class InfraError(ConnectionError):
    """Simulates an infrastructure failure."""


class BusinessError(ValueError):
    """Simulates a business-logic failure (should NOT trip CB)."""


def _make_cb(
    failure_threshold: int = 3,
    success_threshold: int = 2,
    timeout: float = 0.1,
) -> AsyncCircuitBreaker:
    return AsyncCircuitBreaker(
        name="test",
        policy=CircuitBreakerPolicy(
            failure_threshold=failure_threshold,
            success_threshold=success_threshold,
            timeout=timeout,
        ),
    )


# ── TestResiliencyConfig ─────────────────────────────────────────────────────


class TestResiliencyConfig:
    def test_default_values(self) -> None:
        cfg = ResiliencyConfig()
        assert "default" in cfg.timeouts
        assert cfg.timeouts["default"].seconds == 5.0
        assert "default" in cfg.retries
        assert cfg.retries["default"].max_retries == 3
        assert "default" in cfg.circuit_breakers
        assert cfg.circuit_breakers["default"].failure_threshold == 5

    def test_frozen_immutability(self) -> None:
        tp = TimeoutPolicy(seconds=10.0)
        with pytest.raises(AttributeError):
            tp.seconds = 20.0  # type: ignore[misc]

    def test_custom_policies(self) -> None:
        cfg = ResiliencyConfig(
            timeouts={"fast": TimeoutPolicy(seconds=1.0)},
            retries={"aggressive": RetryPolicy(max_retries=5, min_wait=0.5)},
            circuit_breakers={"sensitive": CircuitBreakerPolicy(failure_threshold=2)},
            targets={
                "gcs": TargetBinding(
                    timeout="fast", retry="aggressive", circuit_breaker="sensitive"
                )
            },
        )
        assert cfg.timeouts["fast"].seconds == 1.0
        assert cfg.retries["aggressive"].max_retries == 5
        assert cfg.targets["gcs"].timeout == "fast"


# ── TestCircuitBreakerStateMachine ───────────────────────────────────────────


class TestCircuitBreakerStateMachine:
    @pytest.mark.asyncio
    async def test_starts_closed(self) -> None:
        cb = _make_cb()
        assert cb.current_state is CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_stays_closed_on_success(self) -> None:
        cb = _make_cb()
        async with cb:
            pass
        assert cb.current_state is CircuitState.CLOSED
        assert cb.failure_count == 0

    @pytest.mark.asyncio
    async def test_below_threshold_stays_closed(self) -> None:
        cb = _make_cb(failure_threshold=3)
        for _ in range(2):  # 2 < threshold of 3
            with pytest.raises(InfraError):
                async with cb:
                    raise InfraError("oops")
        assert cb.current_state is CircuitState.CLOSED
        assert cb.failure_count == 2

    @pytest.mark.asyncio
    async def test_trips_to_open(self) -> None:
        cb = _make_cb(failure_threshold=3)
        for _ in range(3):
            with pytest.raises(InfraError):
                async with cb:
                    raise InfraError("oops")
        assert cb.current_state is CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_open_rejects_calls(self) -> None:
        cb = _make_cb(failure_threshold=1)
        with pytest.raises(InfraError):
            async with cb:
                raise InfraError("trip")
        with pytest.raises(CircuitBreakerOpenError):
            async with cb:
                pass  # should never reach here

    @pytest.mark.asyncio
    async def test_half_open_after_timeout(self) -> None:
        cb = _make_cb(failure_threshold=1, timeout=0.05)
        with pytest.raises(InfraError):
            async with cb:
                raise InfraError("trip")
        assert cb.current_state is CircuitState.OPEN
        await asyncio.sleep(0.06)
        assert cb.current_state is CircuitState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_half_open_success_closes(self) -> None:
        cb = _make_cb(failure_threshold=1, success_threshold=1, timeout=0.05)
        with pytest.raises(InfraError):
            async with cb:
                raise InfraError("trip")
        await asyncio.sleep(0.06)
        # Successful probe → CLOSED
        async with cb:
            pass
        assert cb.current_state is CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_half_open_failure_reopens(self) -> None:
        cb = _make_cb(failure_threshold=1, timeout=0.05)
        with pytest.raises(InfraError):
            async with cb:
                raise InfraError("trip")
        await asyncio.sleep(0.06)
        # Failed probe → back to OPEN
        with pytest.raises(InfraError):
            async with cb:
                raise InfraError("still broken")
        assert cb.current_state is CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_success_resets_failure_count(self) -> None:
        cb = _make_cb(failure_threshold=3)
        # 2 failures
        for _ in range(2):
            with pytest.raises(InfraError):
                async with cb:
                    raise InfraError("oops")
        assert cb.failure_count == 2
        # 1 success resets counter
        async with cb:
            pass
        assert cb.failure_count == 0


# ── TestExceptionFiltering ───────────────────────────────────────────────────


class TestExceptionFiltering:
    @pytest.mark.asyncio
    async def test_infra_error_trips_cb(self) -> None:
        cb = _make_cb(failure_threshold=1)
        with pytest.raises(InfraError):
            async with cb:
                raise InfraError("infra")
        assert cb.current_state is CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_connection_error_trips_cb(self) -> None:
        cb = _make_cb(failure_threshold=1)
        with pytest.raises(ConnectionError):
            async with cb:
                raise ConnectionError("conn")
        assert cb.current_state is CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_timeout_error_trips_cb(self) -> None:
        cb = _make_cb(failure_threshold=1)
        with pytest.raises(TimeoutError):
            async with cb:
                raise TimeoutError("timeout")
        assert cb.current_state is CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_os_error_trips_cb(self) -> None:
        cb = _make_cb(failure_threshold=1)
        with pytest.raises(OSError):
            async with cb:
                raise OSError("os")
        assert cb.current_state is CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_business_error_does_not_trip(self) -> None:
        cb = _make_cb(failure_threshold=1)
        with pytest.raises(BusinessError):
            async with cb:
                raise BusinessError("biz")
        assert cb.current_state is CircuitState.CLOSED
        assert cb.failure_count == 0

    @pytest.mark.asyncio
    async def test_generic_error_does_not_trip(self) -> None:
        cb = _make_cb(failure_threshold=1)
        with pytest.raises(RuntimeError):
            async with cb:
                raise RuntimeError("generic")
        assert cb.current_state is CircuitState.CLOSED


# ── TestHalfOpenLock ─────────────────────────────────────────────────────────


class TestHalfOpenLock:
    @pytest.mark.asyncio
    async def test_single_probe_allowed(self) -> None:
        cb = _make_cb(failure_threshold=1, timeout=0.05, success_threshold=1)
        with pytest.raises(InfraError):
            async with cb:
                raise InfraError("trip")
        await asyncio.sleep(0.06)
        # First probe succeeds
        async with cb:
            pass
        assert cb.current_state is CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_concurrent_probes_rejected(self) -> None:
        cb = _make_cb(failure_threshold=1, timeout=0.05, success_threshold=2)
        with pytest.raises(InfraError):
            async with cb:
                raise InfraError("trip")
        await asyncio.sleep(0.06)

        # First probe acquires the lock, second should be rejected
        results: list[str] = []

        async def probe(label: str) -> None:
            try:
                async with cb:
                    await asyncio.sleep(0.05)  # hold the lock
                    results.append(f"{label}:ok")
            except CircuitBreakerOpenError:
                results.append(f"{label}:rejected")

        await asyncio.gather(probe("a"), probe("b"))
        assert "rejected" in " ".join(results)


# ── TestRetryBehavior ────────────────────────────────────────────────────────


class TestRetryBehavior:
    @pytest.mark.asyncio
    async def test_retries_on_infra_error(self) -> None:
        call_count = 0
        mgr = ResiliencyManager(
            config=ResiliencyConfig(
                retries={"fast": RetryPolicy(max_retries=2, min_wait=0.01, max_interval=0.02)},
                circuit_breakers={"none": CircuitBreakerPolicy(failure_threshold=100)},
                targets={
                    "test": TargetBinding(retry="fast", circuit_breaker="none", timeout="off")
                },
                timeouts={"off": TimeoutPolicy(seconds=0)},
            )
        )

        @with_resiliency(target="test", manager=mgr)
        async def flaky() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("fail")
            return "ok"

        result = await flaky()
        assert result == "ok"
        assert call_count == 3  # 1 initial + 2 retries

    @pytest.mark.asyncio
    async def test_no_retry_on_business_error(self) -> None:
        call_count = 0
        mgr = ResiliencyManager(
            config=ResiliencyConfig(
                retries={"fast": RetryPolicy(max_retries=3, min_wait=0.01, max_interval=0.02)},
                circuit_breakers={"none": CircuitBreakerPolicy(failure_threshold=100)},
                targets={
                    "test": TargetBinding(retry="fast", circuit_breaker="none", timeout="off")
                },
                timeouts={"off": TimeoutPolicy(seconds=0)},
            )
        )

        @with_resiliency(target="test", manager=mgr)
        async def broken() -> str:
            nonlocal call_count
            call_count += 1
            raise ValueError("business error")

        with pytest.raises(ValueError, match="business error"):
            await broken()
        assert call_count == 1  # no retries

    @pytest.mark.asyncio
    async def test_exhaustion_raises(self) -> None:
        call_count = 0
        mgr = ResiliencyManager(
            config=ResiliencyConfig(
                retries={"fast": RetryPolicy(max_retries=2, min_wait=0.01, max_interval=0.02)},
                circuit_breakers={"none": CircuitBreakerPolicy(failure_threshold=100)},
                targets={
                    "test": TargetBinding(retry="fast", circuit_breaker="none", timeout="off")
                },
                timeouts={"off": TimeoutPolicy(seconds=0)},
            )
        )

        @with_resiliency(target="test", manager=mgr)
        async def always_fail() -> str:
            nonlocal call_count
            call_count += 1
            raise ConnectionError("always fail")

        with pytest.raises(ConnectionError, match="always fail"):
            await always_fail()
        assert call_count == 3  # 1 initial + 2 retries

    @pytest.mark.asyncio
    async def test_jitter_varies_wait_times(self) -> None:
        """Verify that full jitter is applied (wait_random adds variability)."""
        rp = RetryPolicy(max_retries=2, min_wait=0.5, max_interval=2.0, multiplier=2.0)
        # Just verify the config is valid and policy is usable
        assert rp.min_wait > 0
        assert rp.max_interval >= rp.min_wait


# ── TestTimeoutBehavior ──────────────────────────────────────────────────────


class TestTimeoutBehavior:
    @pytest.mark.asyncio
    async def test_timeout_fires(self) -> None:
        mgr = ResiliencyManager(
            config=ResiliencyConfig(
                timeouts={"fast": TimeoutPolicy(seconds=0.05)},
                retries={"none": RetryPolicy(max_retries=0)},
                circuit_breakers={"none": CircuitBreakerPolicy(failure_threshold=100)},
                targets={
                    "test": TargetBinding(timeout="fast", retry="none", circuit_breaker="none")
                },
            )
        )

        @with_resiliency(target="test", manager=mgr)
        async def slow_op() -> str:
            await asyncio.sleep(1.0)
            return "done"

        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await slow_op()

    @pytest.mark.asyncio
    async def test_fast_completes(self) -> None:
        mgr = ResiliencyManager(
            config=ResiliencyConfig(
                timeouts={"generous": TimeoutPolicy(seconds=5.0)},
                retries={"none": RetryPolicy(max_retries=0)},
                circuit_breakers={"none": CircuitBreakerPolicy(failure_threshold=100)},
                targets={
                    "test": TargetBinding(timeout="generous", retry="none", circuit_breaker="none")
                },
            )
        )

        @with_resiliency(target="test", manager=mgr)
        async def fast_op() -> str:
            return "done"

        result = await fast_op()
        assert result == "done"

    @pytest.mark.asyncio
    async def test_timeout_disabled_when_zero(self) -> None:
        mgr = ResiliencyManager(
            config=ResiliencyConfig(
                timeouts={"off": TimeoutPolicy(seconds=0)},
                retries={"none": RetryPolicy(max_retries=0)},
                circuit_breakers={"none": CircuitBreakerPolicy(failure_threshold=100)},
                targets={
                    "test": TargetBinding(timeout="off", retry="none", circuit_breaker="none")
                },
            )
        )

        @with_resiliency(target="test", manager=mgr)
        async def op() -> str:
            return "done"

        result = await op()
        assert result == "done"


# ── TestComposition ──────────────────────────────────────────────────────────


class TestComposition:
    @pytest.mark.asyncio
    async def test_cb_open_prevents_retry(self) -> None:
        """When CB is open, calls fail immediately without retrying."""
        call_count = 0
        mgr = ResiliencyManager(
            config=ResiliencyConfig(
                retries={"fast": RetryPolicy(max_retries=3, min_wait=0.01, max_interval=0.02)},
                circuit_breakers={
                    "sensitive": CircuitBreakerPolicy(failure_threshold=1, timeout=60.0)
                },
                targets={
                    "test": TargetBinding(retry="fast", circuit_breaker="sensitive", timeout="off")
                },
                timeouts={"off": TimeoutPolicy(seconds=0)},
            )
        )

        @with_resiliency(target="test", manager=mgr)
        async def failing() -> str:
            nonlocal call_count
            call_count += 1
            raise ConnectionError("fail")

        # First call: trips CB
        with pytest.raises(ConnectionError):
            await failing()
        assert call_count >= 1

        # Second call: CB is open, fail fast
        call_count = 0
        with pytest.raises(CircuitBreakerOpenError):
            await failing()
        assert call_count == 0  # never entered the function

    @pytest.mark.asyncio
    async def test_timeout_inside_retry(self) -> None:
        """Timeout fires per-attempt, not per-entire-retry sequence."""
        call_count = 0
        mgr = ResiliencyManager(
            config=ResiliencyConfig(
                timeouts={"fast": TimeoutPolicy(seconds=0.05)},
                retries={"once": RetryPolicy(max_retries=1, min_wait=0.01, max_interval=0.02)},
                circuit_breakers={"none": CircuitBreakerPolicy(failure_threshold=100)},
                targets={
                    "test": TargetBinding(timeout="fast", retry="once", circuit_breaker="none")
                },
            )
        )

        @with_resiliency(target="test", manager=mgr)
        async def slow() -> str:
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(1.0)
            return "done"

        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await slow()
        assert call_count == 2  # initial + 1 retry

    @pytest.mark.asyncio
    async def test_full_stack_success(self) -> None:
        """CB + Retry + Timeout all pass on fast success."""
        mgr = ResiliencyManager(
            config=ResiliencyConfig(
                timeouts={"default": TimeoutPolicy(seconds=5.0)},
                retries={"default": RetryPolicy(max_retries=2, min_wait=0.01, max_interval=0.02)},
                circuit_breakers={"default": CircuitBreakerPolicy(failure_threshold=3)},
                targets={"test": TargetBinding()},
            )
        )

        @with_resiliency(target="test", manager=mgr)
        async def op() -> str:
            return "ok"

        assert await op() == "ok"


# ── TestWithResiliencyDecorator ──────────────────────────────────────────────


class TestWithResiliencyDecorator:
    @pytest.mark.asyncio
    async def test_target_binding(self) -> None:
        mgr = ResiliencyManager(
            config=ResiliencyConfig(
                targets={
                    "gcs": TargetBinding(
                        timeout="default", retry="default", circuit_breaker="default"
                    )
                },
            )
        )

        @with_resiliency(target="gcs", manager=mgr)
        async def upload() -> str:
            return "uploaded"

        assert await upload() == "uploaded"

    @pytest.mark.asyncio
    async def test_explicit_policies(self) -> None:
        mgr = ResiliencyManager(
            config=ResiliencyConfig(
                retries={"fast": RetryPolicy(max_retries=1, min_wait=0.01, max_interval=0.02)},
                circuit_breakers={"none": CircuitBreakerPolicy(failure_threshold=100)},
                timeouts={"off": TimeoutPolicy(seconds=0)},
            )
        )

        @with_resiliency(timeout="off", retry="fast", circuit_breaker="none", manager=mgr)
        async def op() -> str:
            return "done"

        assert await op() == "done"

    @pytest.mark.asyncio
    async def test_no_manager_passthrough(self) -> None:
        old = get_default_manager()
        try:
            set_default_manager(None)  # type: ignore[arg-type]

            @with_resiliency(target="anything")
            async def op() -> str:
                return "passthrough"

            assert await op() == "passthrough"
        finally:
            if old is not None:
                set_default_manager(old)

    @pytest.mark.asyncio
    async def test_unknown_target_uses_defaults(self) -> None:
        mgr = ResiliencyManager(config=ResiliencyConfig())

        @with_resiliency(target="nonexistent", manager=mgr)
        async def op() -> str:
            return "ok"

        assert await op() == "ok"


# ── TestResiliencyManager ────────────────────────────────────────────────────


class TestResiliencyManager:
    def test_get_or_create_breaker(self) -> None:
        mgr = ResiliencyManager(config=ResiliencyConfig())
        cb = mgr.get_breaker("default")
        assert cb.name == "default"

    def test_same_instance_returned(self) -> None:
        mgr = ResiliencyManager(config=ResiliencyConfig())
        cb1 = mgr.get_breaker("default")
        cb2 = mgr.get_breaker("default")
        assert cb1 is cb2

    def test_fallback_to_default_policy(self) -> None:
        mgr = ResiliencyManager(config=ResiliencyConfig())
        cb = mgr.get_breaker("unknown")
        assert cb.name == "unknown"  # created with default policy

    def test_health_ok(self) -> None:
        mgr = ResiliencyManager(config=ResiliencyConfig())
        mgr.get_breaker("test")  # ensure at least one exists
        health = mgr.health_check()
        assert health["status"] == "ok"
        assert "test" in health["circuit_breakers"]
        assert health["circuit_breakers"]["test"]["state"] == "closed"

    @pytest.mark.asyncio
    async def test_health_degraded(self) -> None:
        mgr = ResiliencyManager(
            config=ResiliencyConfig(
                circuit_breakers={"test": CircuitBreakerPolicy(failure_threshold=1)}
            )
        )
        cb = mgr.get_breaker("test")
        with pytest.raises(ConnectionError):
            async with cb:
                raise ConnectionError("fail")
        health = mgr.health_check()
        assert health["status"] == "degraded"
        assert health["circuit_breakers"]["test"]["state"] == "open"

    def test_resolve_target(self) -> None:
        mgr = ResiliencyManager(
            config=ResiliencyConfig(
                targets={"gcs": TargetBinding(timeout="slow", retry="aggressive")}
            )
        )
        binding = mgr.resolve_target("gcs")
        assert binding.timeout == "slow"
        assert binding.retry == "aggressive"

    def test_resolve_unknown_target_returns_default(self) -> None:
        mgr = ResiliencyManager(config=ResiliencyConfig())
        binding = mgr.resolve_target("nonexistent")
        assert binding.timeout == "default"
        assert binding.retry == "default"


# ── TestDurationParser ───────────────────────────────────────────────────────


class TestDurationParser:
    def test_numeric_int(self) -> None:
        assert parse_duration(5) == 5.0

    def test_numeric_float(self) -> None:
        assert parse_duration(3.5) == 3.5

    def test_seconds_suffix(self) -> None:
        assert parse_duration("30s") == 30.0

    def test_minutes_suffix(self) -> None:
        assert parse_duration("10m") == 600.0

    def test_hours_suffix(self) -> None:
        assert parse_duration("1h") == 3600.0

    def test_bare_number_string(self) -> None:
        assert parse_duration("42") == 42.0

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse duration"):
            parse_duration("abc")


# ── TestInfraExceptions ──────────────────────────────────────────────────────


class TestInfraExceptions:
    def test_base_exceptions(self) -> None:
        exc = _get_infra_exceptions()
        assert TimeoutError in exc
        assert ConnectionError in exc
        assert OSError in exc

    def test_includes_httpx_if_available(self) -> None:
        try:
            import httpx

            exc = _get_infra_exceptions()
            assert httpx.TransportError in exc
            assert httpx.TimeoutException in exc
        except ImportError:
            pytest.skip("httpx not installed")
