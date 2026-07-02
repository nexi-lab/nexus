#!/usr/bin/env python3
"""Wedge probe for a live filesystem mount point.

Exercises the syscalls that fs-vulnerable tests will fire (stat +
mkdir + rmdir) via ``subprocess.run(timeout=...)`` so a wedged
kernel-fs backend surfaces as a Python-bounded timeout, not an
indefinite kernel-level block.  Used by CI workflows as a pre-pytest
gate — see ``.github/workflows/fuse-plugin-macos-nfs-e2e.yml``.

Why a standalone script (not inline in the workflow):
  * GNU ``timeout`` isn't on macOS runners by default; ``coreutils``
    would add a brew install to every workflow that wants a wedge
    probe.  Python is already installed by the setup-python step,
    so a shared script has zero extra deps.
  * A YAML ``run: |`` heredoc with Python content confuses YAML
    parsers (indentation of Python's ``def`` bodies clashes with
    the block-scalar's own indent).  A separate .py file avoids
    the escape hell.
  * Sharable by Docker E2E workflows — same failure class, same fix.

Usage:
    python3 scripts/wedge_probe.py <mount-point> [--per-op-timeout SECS]

Exit code:
    0 — all syscalls responded within their timeout, mount is healthy.
    1 — one or more syscalls timed out or errored; diagnostic printed
        to stdout in GHA ``::error::`` format so the workflow log
        surfaces the failure at the right log-viewer level.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import uuid


def _bounded(cmd: list[str], *, timeout: float) -> bool:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"::error::wedge probe: {' '.join(cmd)} timed out after {timeout}s")
        return False
    except FileNotFoundError:
        print(f"::error::wedge probe: command not found: {cmd[0]}")
        return False
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        print(f"::error::wedge probe: {' '.join(cmd)} rc={proc.returncode} stderr={stderr!r}")
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mount", help="mount point to probe")
    parser.add_argument(
        "--per-op-timeout",
        type=float,
        default=5.0,
        help="Per-syscall timeout in seconds (default 5).",
    )
    args = parser.parse_args()

    probe_dir = f"{args.mount}/.wedge-probe-{uuid.uuid4().hex[:8]}"
    checks: list[list[str]] = [
        ["stat", args.mount],
        ["mkdir", "-p", probe_dir],
        ["rmdir", probe_dir],
    ]
    ok = all(_bounded(cmd, timeout=args.per_op_timeout) for cmd in checks)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
