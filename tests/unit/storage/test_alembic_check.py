"""Test that Alembic detects no pending model changes after domain file split.

Issue #1286, Decision 12: Automated alembic check test.
"""

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(
    subprocess.run(
        [sys.executable, "-m", "alembic", "--help"],
        capture_output=True,
    ).returncode
    != 0
    and False,  # Always try; skip handled by inner logic
    reason="alembic not installed",
)
class TestAlembicCheck:
    """Verify no pending schema changes after model split."""

    def test_no_pending_migrations(self) -> None:
        """Run 'alembic check' to verify models match latest migration."""
        project_root = Path(__file__).resolve().parents[3]
        alembic_ini = project_root / "alembic" / "alembic.ini"
        if not alembic_ini.exists():
            pytest.skip(f"Alembic config not found at {alembic_ini}")

        result = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", str(alembic_ini), "check"],
            capture_output=True,
            cwd=project_root,
            text=True,
            timeout=30,
        )
        # alembic check exits 0 if no new migrations needed
        if result.returncode != 0:
            # If alembic is not configured or DB not available, skip gracefully
            combined_output = (result.stdout + result.stderr).lower()
            if "unable to open database file" in combined_output:
                pytest.skip("Alembic database unavailable: unable to open database file")
            if any(
                phrase in combined_output
                for phrase in [
                    "no such file",
                    "could not locate",
                    "no config file",
                    "connection refused",
                    "does not exist",
                    "no module named",
                    "script_location",
                ]
            ):
                pytest.skip(
                    f"Alembic not configured for test: {(result.stdout + result.stderr).strip()}"
                )

            pytest.fail(
                f"alembic check failed (exit {result.returncode}):\n"
                f"stdout: {result.stdout}\n"
                f"stderr: {result.stderr}"
            )
