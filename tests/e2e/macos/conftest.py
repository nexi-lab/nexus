"""Conftest for macOS FUSE/NFS filesystem E2E.

Every test in this directory touches a live macOS FUSE-T or NFS
mount point via kernel VFS syscalls (``os.path.isdir``, ``open``,
``os.rename``).  Those calls can block indefinitely at the kernel
level when the backend is wedged — FUSE-T stalls on Apple Silicon
NFS runner are documented in memory ``reference_fuse_t_x86_64_nfs_hang``.
Without per-test intervention the ONLY recovery is the workflow's
30-minute wall-clock timeout, which:

  * produces no diagnostic (test just "cancelled");
  * burns 30 minutes of runner budget per hit;
  * masks the actual failure surface (wedged mount vs test bug vs
    daemon crash) inside a generic ``##[error]The operation was
    canceled`` line.

This conftest applies a systematic fix at the *directory* granularity
so no individual test opts in:

  * every test gets ``@pytest.mark.wedge_watchdog(seconds=60)``;
  * every test failure (including watchdog timeout) captures live
    diagnostics (mount table, ps, log tail) into the CI log so
    triage doesn't need artifact download.

Applies to the ENTIRE directory because every test here is
fs-vulnerable by definition (this suite exercises the FUSE plugin
mount path).  Non-fs tests should not be added to this directory.
"""

from __future__ import annotations

import os
import subprocess

import pytest

# Default watchdog budget for tests in this directory.  Real tests
# complete in <2s; 60s is the point at which a wedge is definitely
# not "slow test needs profiling".
_DEFAULT_WATCHDOG_SECONDS = 60


def pytest_collection_modifyitems(config, items):
    """Auto-apply the wedge_watchdog marker to every test in this dir.

    ``config`` + ``items`` are pytest's collection hook.  We check the
    item's node id starts with the macOS E2E path prefix so nested
    imports (rare) don't accidentally get the watchdog when they
    shouldn't.
    """
    for item in items:
        # Item nodeid uses forward slashes on both POSIX and Windows.
        nodeid_norm = item.nodeid.replace("\\", "/")
        if not nodeid_norm.startswith("tests/e2e/macos/"):
            continue
        # Skip if the test explicitly set its own wedge_watchdog with
        # a different budget — respect per-test overrides.
        if item.get_closest_marker("wedge_watchdog") is not None:
            continue
        item.add_marker(pytest.mark.wedge_watchdog(seconds=_DEFAULT_WATCHDOG_SECONDS))


def _run(cmd: list[str], timeout: float = 5.0) -> str:
    """Run a diagnostic command with a hard subprocess timeout.

    Wedge-safe: if the diagnostic itself queries a stalled fs (``mount``
    can hang on a wedged fs on some macOS versions), the subprocess
    timeout kills it and we surface a ``[TIMEOUT]`` marker instead of
    the diagnostic infinitely blocking the failure report.
    """
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"[TIMEOUT after {timeout}s]"
    except FileNotFoundError:
        return f"[COMMAND NOT FOUND: {cmd[0]}]"
    out = (proc.stdout or "") + (proc.stderr or "")
    return out.strip() or "[empty]"


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    """Capture wedge diagnostics on failure — including watchdog timeout.

    Runs AFTER pytest builds the normal report.  For failures or errors
    in the ``call`` phase (where the wedge would fire), dumps:

      * ``mount`` output — is the FUSE mount table still consistent?
      * ``ls`` of the mount point with 2s subprocess-timeout — does
        the fs respond at all?
      * ``ps -o pid,stat,command -p <daemon_pid>`` if pid available —
        did the daemon crash / go into uninterruptible-wait state?

    Output goes to the section that ``-v`` mode shows post-summary,
    so CI logs surface the wedge state right where the test failure
    is reported.  No artifact download needed for triage.
    """
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or report.passed:
        return
    mount_point = os.environ.get("NEXUS_FUSE_MOUNT_POINT", "/tmp/nexus-nfs-e2e")
    diagnostic_lines = [
        "── wedge diagnostic (post-failure) ──",
        f"[mount table]\n{_run(['mount'])}",
        f"[ls mount, 2s timeout]\n{_run(['ls', '-la', mount_point], timeout=2.0)}",
    ]
    daemon_pid = os.environ.get("NEXUSD_PID")
    if daemon_pid:
        diagnostic_lines.append(
            f"[daemon ps pid={daemon_pid}]\n{_run(['ps', '-o', 'pid,stat,command', '-p', daemon_pid])}"
        )
    report.sections.append(("wedge diagnostic", "\n".join(diagnostic_lines)))
