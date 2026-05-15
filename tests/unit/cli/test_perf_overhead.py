"""P2: Per-invocation overhead measurement for CLI output infrastructure.

Measures the baseline import/startup cost of the CLI and the overhead added by
``add_output_options``, ``CommandTiming``, and ``render_output``.  Uses
``time.perf_counter()`` for high-resolution timing without extra dependencies.
"""

from __future__ import annotations

import importlib
import io
import sys
import time
from contextlib import redirect_stdout
from typing import Any

import click

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ITERATIONS = 100


def _avg_us(elapsed_s: float, iterations: int = ITERATIONS) -> float:
    """Return average elapsed time in *microseconds*."""
    return (elapsed_s / iterations) * 1_000_000


def _avg_ms(elapsed_s: float, iterations: int = ITERATIONS) -> float:
    """Return average elapsed time in *milliseconds*."""
    return (elapsed_s / iterations) * 1_000


# ---------------------------------------------------------------------------
# Import-time benchmarks
# ---------------------------------------------------------------------------


class TestImportOverhead:
    """Measure import latency for key CLI modules."""

    def test_import_time_output_module(self) -> None:
        """Import nexus.cli.output should complete in < 100 ms on average."""
        times: list[float] = []
        for _ in range(ITERATIONS):
            importlib.invalidate_caches()
            mod = sys.modules.pop("nexus.cli.output", None)  # noqa: F841
            start = time.perf_counter()
            importlib.import_module("nexus.cli.output")
            times.append(time.perf_counter() - start)

        avg_ms = _avg_ms(sum(times), ITERATIONS)
        # Generous threshold: 2x the 100 ms target
        assert avg_ms < 200, f"Average import time {avg_ms:.2f} ms exceeds 200 ms threshold"

    def test_import_time_timing_module(self) -> None:
        """Import nexus.cli.timing should complete in < 50 ms on average."""
        times: list[float] = []
        for _ in range(ITERATIONS):
            importlib.invalidate_caches()
            mod = sys.modules.pop("nexus.cli.timing", None)  # noqa: F841
            start = time.perf_counter()
            importlib.import_module("nexus.cli.timing")
            times.append(time.perf_counter() - start)

        avg_ms = _avg_ms(sum(times), ITERATIONS)
        # Generous threshold: 2x the 50 ms target
        assert avg_ms < 100, f"Average import time {avg_ms:.2f} ms exceeds 100 ms threshold"

    def test_cli_entry_point_import(self) -> None:
        """Import nexus.cli.main -- documents total import cost.

        No hard assertion; this test records the time so regressions are
        visible in test output.
        """
        times: list[float] = []
        for _ in range(ITERATIONS):
            importlib.invalidate_caches()
            mod = sys.modules.pop("nexus.cli.main", None)  # noqa: F841
            start = time.perf_counter()
            importlib.import_module("nexus.cli.main")
            times.append(time.perf_counter() - start)

        avg_ms = _avg_ms(sum(times), ITERATIONS)
        # Document, don't gate -- main pulls in the entire command tree.
        # Use a very generous 2-second ceiling to catch catastrophic regressions.
        assert avg_ms < 2000, f"Main entry-point import {avg_ms:.2f} ms exceeds 2 000 ms ceiling"


# ---------------------------------------------------------------------------
# Creation overhead
# ---------------------------------------------------------------------------


class TestCreationOverhead:
    """Measure object-creation cost for core CLI helpers."""

    def test_output_options_creation_overhead(self) -> None:
        """Creating an OutputOptions instance should take < 1 ms."""
        from nexus.cli.output import OutputOptions

        start = time.perf_counter()
        for _ in range(ITERATIONS):
            OutputOptions(
                json_output=False,
                quiet=False,
                verbosity=0,
                fields=None,
                request_id="perf-test",
            )
        elapsed = time.perf_counter() - start

        avg_ms = _avg_ms(elapsed)
        # 2x the 1 ms target
        assert avg_ms < 2, f"Average OutputOptions creation {avg_ms:.4f} ms exceeds 2 ms"

    def test_command_timing_overhead(self) -> None:
        """CommandTiming() + one .phase() context manager should take < 1 ms."""
        from nexus.cli.timing import CommandTiming

        start = time.perf_counter()
        for _ in range(ITERATIONS):
            timing = CommandTiming()
            with timing.phase("test"):
                pass
        elapsed = time.perf_counter() - start

        avg_ms = _avg_ms(elapsed)
        # 2x the 1 ms target
        assert avg_ms < 2, f"Average CommandTiming overhead {avg_ms:.4f} ms exceeds 2 ms"


# ---------------------------------------------------------------------------
# Render overhead
# ---------------------------------------------------------------------------


class TestRenderOverhead:
    """Measure render_output cost for JSON and human formatters."""

    def test_render_output_json_overhead(self) -> None:
        """render_output with json_output=True for a small dict: < 5 ms."""
        from nexus.cli.output import OutputOptions, render_output

        opts = OutputOptions(
            json_output=True,
            quiet=False,
            verbosity=0,
            fields=None,
            request_id="perf-test",
        )
        data: dict[str, Any] = {"path": "/test.txt", "size": 42, "ok": True}

        start = time.perf_counter()
        for _ in range(ITERATIONS):
            with redirect_stdout(io.StringIO()):
                render_output(data=data, output_opts=opts)
        elapsed = time.perf_counter() - start

        avg_ms = _avg_ms(elapsed)
        # 2x the 5 ms target
        assert avg_ms < 10, f"Average JSON render {avg_ms:.4f} ms exceeds 10 ms"

    def test_render_output_human_overhead(self) -> None:
        """render_output with json_output=False and a simple formatter: < 5 ms."""
        from nexus.cli.output import OutputOptions, render_output

        opts = OutputOptions(
            json_output=False,
            quiet=False,
            verbosity=0,
            fields=None,
            request_id="perf-test",
        )
        data: dict[str, Any] = {"path": "/test.txt", "size": 42}

        def human_formatter(d: Any) -> None:
            click.echo(f"{d['path']}  {d['size']} bytes")

        start = time.perf_counter()
        for _ in range(ITERATIONS):
            with redirect_stdout(io.StringIO()):
                render_output(
                    data=data,
                    output_opts=opts,
                    human_formatter=human_formatter,
                )
        elapsed = time.perf_counter() - start

        avg_ms = _avg_ms(elapsed)
        # 2x the 5 ms target
        assert avg_ms < 10, f"Average human render {avg_ms:.4f} ms exceeds 10 ms"


# ---------------------------------------------------------------------------
# Decorator overhead
# ---------------------------------------------------------------------------


class TestDecoratorOverhead:
    """Measure the cost of applying @add_output_options."""

    def test_add_output_options_decorator_overhead(self) -> None:
        """Applying @add_output_options to a dummy command: < 5 ms."""
        from nexus.cli.output import add_output_options

        @click.command("perf-dummy")
        @click.pass_context
        def _base_cmd(ctx: click.Context, output_opts: Any) -> None:  # noqa: ARG001
            pass

        start = time.perf_counter()
        for _ in range(ITERATIONS):
            # Re-create the base command each iteration so the decorator
            # actually runs rather than returning a cached wrapper.
            @click.command(f"perf-{_}")
            @click.pass_context
            def _inner(ctx: click.Context, output_opts: Any) -> None:  # noqa: ARG001
                pass

            add_output_options(_inner)
        elapsed = time.perf_counter() - start

        avg_ms = _avg_ms(elapsed)
        # 2x the 5 ms target
        assert avg_ms < 10, f"Average decorator overhead {avg_ms:.4f} ms exceeds 10 ms"
