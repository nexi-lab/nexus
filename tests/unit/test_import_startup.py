"""Import startup time benchmarks (Issue #1291, Decision 15A).

Subprocess-based tests that measure import time for key modules.
These are guardrails, not micro-benchmarks — they catch accidental
heavy-import regressions, not 1ms differences.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


def _measure_import_time(module: str, timeout: float = 10.0) -> float:
    """Import a module in a subprocess and return wall-clock time in seconds."""
    code = f"""
import time
start = time.monotonic()
import {module}
elapsed = time.monotonic() - start
print(f"{{elapsed:.6f}}")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={"PYTHONPATH": "src"},
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[2]),
    )
    if result.returncode != 0:
        pytest.fail(f"Import of {module} failed: {result.stderr}")
    return float(result.stdout.strip())


class TestImportStartup:
    """Startup time guardrails for key modules."""

    def test_types_import_fast(self) -> None:
        """core.types (leaf module) should import quickly."""
        elapsed = _measure_import_time("nexus.core.types")
        # Leaf module with only stdlib deps — should be well under 1s
        assert elapsed < 1.0, f"nexus.core.types took {elapsed:.3f}s (expected < 1.0s)"

    def test_permissions_import(self) -> None:
        """core.permissions imports without error."""
        elapsed = _measure_import_time("nexus.core.permissions")
        # Permissions module is larger but should still be reasonable
        assert elapsed < 5.0, f"nexus.core.permissions took {elapsed:.3f}s (expected < 5.0s)"

    def test_factory_import(self) -> None:
        """factory module imports without error (full DI chain)."""
        elapsed = _measure_import_time("nexus.factory")
        # Factory pulls in many deps — just ensure it completes
        assert elapsed < 10.0, f"nexus.factory took {elapsed:.3f}s (expected < 10.0s)"
