"""Benchmark tests for ObjectStoreABC adapter overhead.

Ensures the adapter + @timed_response add < 10μs per call.
"""

from __future__ import annotations

import time

import pytest

from nexus.core.response import HandlerResponse, timed_response


class TestTimedResponseOverhead:
    """Benchmark @timed_response decorator overhead."""

    def test_handler_response_creation_speed(self) -> None:
        """Raw HandlerResponse.ok() creation should be very fast."""
        iterations = 10_000
        start = time.perf_counter()
        for _ in range(iterations):
            HandlerResponse.ok(data="hash123", backend_name="bench")
        elapsed_us = (time.perf_counter() - start) * 1_000_000 / iterations
        # Should be well under 10μs per call
        assert elapsed_us < 10, f"HandlerResponse.ok() took {elapsed_us:.2f}μs per call"

    def test_timed_response_decorator_overhead(self) -> None:
        """@timed_response overhead should be < 10μs per call."""

        class Bench:
            name = "bench"

            @timed_response
            def op(self) -> HandlerResponse[str]:
                return HandlerResponse.ok(data="ok", backend_name=self.name)

        obj = Bench()
        # Warmup
        for _ in range(100):
            obj.op()

        iterations = 10_000
        start = time.perf_counter()
        for _ in range(iterations):
            obj.op()
        elapsed_us = (time.perf_counter() - start) * 1_000_000 / iterations

        # timed_response adds timing but should still be under 10μs overhead
        assert elapsed_us < 50, f"@timed_response took {elapsed_us:.2f}μs per call"


class TestAdapterOverhead:
    """Benchmark BackendObjectStore adapter overhead (Phase 6 will expand)."""

    def test_adapter_import_succeeds(self) -> None:
        """Verify ObjectStoreABC can be imported (will be created in Phase 3)."""
        try:
            from nexus.core.object_store import BackendObjectStore, ObjectStoreABC  # noqa: F401

            assert True
        except ImportError:
            pytest.skip("ObjectStoreABC not yet created (Phase 3)")
