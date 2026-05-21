from __future__ import annotations

import io

from tests.e2e.self_contained.cli import test_sandbox_federation_e2e as sandbox_e2e


class _ExitedProcess:
    returncode = 70

    def __init__(self, stderr: str) -> None:
        self.stderr = io.StringIO(stderr)

    def poll(self) -> int:
        return self.returncode


def test_health_poll_reports_daemon_exit_before_timeout() -> None:
    proc = _ExitedProcess("Error: Failed to initialize NexusFS: 'nexus-cluster'")

    result = sandbox_e2e._poll_health_until_ready_or_exit(
        "http://127.0.0.1:9/health",
        proc,
        timeout=60,
    )

    assert result.ready is False
    assert result.exited is True
    assert result.returncode == 70
    assert "nexus-cluster" in result.stderr


def test_kernel_missing_skip_reason_matches_late_daemon_exit() -> None:
    reason = sandbox_e2e._kernel_missing_skip_reason(
        "Error: Failed to initialize NexusFS: [Errno 2] No such file or directory: 'nexus-cluster'"
    )

    assert reason
    assert "nexus-cluster" in reason


def test_kernel_missing_skip_reason_does_not_mask_unrelated_import_error() -> None:
    reason = sandbox_e2e._kernel_missing_skip_reason(
        "ModuleNotFoundError: No module named 'nexus.bricks.some_dependency'"
    )

    assert reason is None
