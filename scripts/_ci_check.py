#!/usr/bin/env python3
"""Tiny one-shot CI status checker for Monitor.

Prints exactly one of:
  RUNNING_<done>/<total>_failed_<n>_state_<state>
  FAIL:<check_name>
  READY_TO_MERGE
  BEHIND_NEED_REBASE

Usage: python scripts/_ci_check.py <PR_NUM>
"""

from __future__ import annotations

import json
import subprocess
import sys


def main() -> None:
    pr_num = sys.argv[1]
    out = subprocess.check_output(
        ["gh", "pr", "view", pr_num, "--json", "mergeable,mergeStateStatus,statusCheckRollup"],
        timeout=60,
    )
    d = json.loads(out)
    checks = d["statusCheckRollup"]
    total = len(checks)
    done = sum(1 for c in checks if c.get("status") == "COMPLETED")
    failed = [
        c["name"]
        for c in checks
        if c.get("conclusion")
        in ("FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE")
    ]
    state = d.get("mergeStateStatus", "?")
    print(f"RUNNING_{done}/{total}_failed_{len(failed)}_state_{state}")
    for n in failed:
        print(f"FAIL:{n}")
    if done == total and not failed:
        if state == "CLEAN":
            print("READY_TO_MERGE")
        elif state == "BEHIND":
            print("BEHIND_NEED_REBASE")


if __name__ == "__main__":
    main()
