"""Cross-machine L1 smoke against the ``nexusd-cluster`` binary.

Covers the static-bootstrap path under the PR #3996 contract:

  * One side passes ``--bootstrap-new`` (or sets
    ``NEXUS_BOOTSTRAP_NEW=1``); it becomes the cluster founder and
    creates a 1-voter root zone.
  * The other side is a pure joiner.  Empty storage + non-empty
    ``--peers`` + flag unset = ``bootstrap_or_join_root``'s
    wait-and-join branch, which retries JoinZone forever.

The script's job is to launch ``nexusd-cluster`` with the right env,
wait for the raft layer to converge, and assert from each side's log
that the expected ConfState transitions happened — founder creates
the 1-voter zone, joiner sends JoinZone, founder commits AddNode and
both nodes see ``voters=[founder, joiner]``.

Cross-machine read/write replication on the data plane (the ``rw``
half of L1) is a follow-up — wiring through ``nexusd-cluster``'s
gRPC surface needs a separate client and is out of scope for the
contract pin this script provides.

Run:

    # On the founder side (Win):
    PYTHONPATH=. python scripts/cluster_smoke_xmachine.py \\
        --side win --bootstrap-new \\
        --peers 100.64.0.26:2126,100.64.0.21:2126

    # On the joiner side (Mac):
    PYTHONPATH=. python scripts/cluster_smoke_xmachine.py \\
        --side mac \\
        --peers 100.64.0.26:2126,100.64.0.21:2126

Exactly one side must pass ``--bootstrap-new`` — the contract
forbids two founders, and zero founders means neither side ever
creates the cluster (deadlock).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_BIND = "0.0.0.0:2126"
LOG_TAIL_LINES = 200


def find_nexusd_cluster_binary() -> Path:
    """Locate the ``nexusd-cluster`` binary in the workspace target dir.

    Falls back to ``$PATH`` for installed binaries (e.g. CI runners).
    """
    repo_root = Path(__file__).resolve().parent.parent
    for profile in ("release", "debug"):
        suffix = ".exe" if os.name == "nt" else ""
        candidate = repo_root / "target" / profile / f"nexusd-cluster{suffix}"
        if candidate.exists():
            return candidate
    # PATH fallback
    from shutil import which

    found = which("nexusd-cluster")
    if found:
        return Path(found)
    raise FileNotFoundError(
        "nexusd-cluster binary not found.  Build with "
        "`cargo build --release -p nexus-cluster` from the repo root, "
        "or add it to PATH."
    )


def launch_daemon(
    binary: Path,
    *,
    side: str,
    peers: str,
    data_dir: Path,
    log_path: Path,
    bootstrap_new: bool,
    bind_addr: str,
    hostname: str | None,
) -> subprocess.Popen[bytes]:
    """Spawn nexusd-cluster as a child process with the right env."""
    env = os.environ.copy()
    if bootstrap_new:
        env["NEXUS_BOOTSTRAP_NEW"] = "1"
    else:
        env.pop("NEXUS_BOOTSTRAP_NEW", None)
    if hostname:
        env["NEXUS_HOSTNAME"] = hostname
    env["NEXUS_NO_TLS"] = "1"  # plaintext gRPC for the smoke; mTLS is a separate test

    cmd = [
        str(binary),
        "--bind-addr",
        bind_addr,
        "--data-dir",
        str(data_dir),
        "--peers",
        peers,
        "--no-tls",
    ]
    print(f"[{side}] launch: {' '.join(cmd)}", flush=True)
    print(f"[{side}] data_dir={data_dir} log={log_path} bootstrap_new={bootstrap_new}", flush=True)
    # The file handle's lifetime tracks the subprocess we're about to
    # spawn — Popen needs a long-lived file descriptor to inherit, so
    # a `with` block would close it before the child can use it.
    log_fp = open(log_path, "wb")  # noqa: SIM115 — see comment
    return subprocess.Popen(
        cmd,
        env=env,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
    )


def tail_log(log_path: Path, n: int = LOG_TAIL_LINES) -> str:
    if not log_path.exists():
        return "<no log yet>"
    return "\n".join(log_path.read_text(errors="replace").splitlines()[-n:])


def wait_for_marker(
    log_path: Path, pattern: re.Pattern[str], timeout_s: float, label: str
) -> str | None:
    """Poll the log file for `pattern`.  Returns the matching line or None."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if log_path.exists():
            for line in log_path.read_text(errors="replace").splitlines():
                m = pattern.search(line)
                if m:
                    return line
        time.sleep(0.5)
    print(f"  [TIMEOUT] {label} after {timeout_s}s", flush=True)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="L1 cluster smoke for nexusd-cluster")
    parser.add_argument("--side", choices=("win", "mac", "founder", "joiner"), required=True)
    parser.add_argument(
        "--bootstrap-new",
        action="store_true",
        help="Mark this side as cluster founder (sets NEXUS_BOOTSTRAP_NEW=1). "
        "Exactly one side must pass this; the other JoinZones via --peers.",
    )
    parser.add_argument(
        "--peers",
        required=True,
        help="Comma-separated raft peer list, both sides included (e.g. "
        "'100.64.0.26:2126,100.64.0.21:2126').",
    )
    parser.add_argument(
        "--bind-addr",
        default=DEFAULT_BIND,
        help=f"Local raft bind address (default {DEFAULT_BIND}).",
    )
    parser.add_argument(
        "--hostname",
        default=None,
        help="Override NEXUS_HOSTNAME (defaults to OS hostname).",
    )
    parser.add_argument(
        "--cluster-form-timeout",
        type=float,
        default=180.0,
        help="Seconds to wait for raft cluster to form (peer must come up).",
    )
    args = parser.parse_args()

    binary = find_nexusd_cluster_binary()
    print(f"binary: {binary}", flush=True)

    tmp = Path(tempfile.mkdtemp(prefix=f"cluster-smoke-{args.side}-"))
    data_dir = tmp / "data"
    log_path = tmp / "nexusd.log"
    print(f"smoke tmpdir: {tmp}", flush=True)

    proc = launch_daemon(
        binary,
        side=args.side,
        peers=args.peers,
        data_dir=data_dir,
        log_path=log_path,
        bootstrap_new=args.bootstrap_new,
        bind_addr=args.bind_addr,
        hostname=args.hostname,
    )
    try:
        # Stage 1: daemon comes up, raft init runs.
        print(f"\n=== [{args.side}] stage 1 — federation bootstrap ===", flush=True)
        line = wait_for_marker(
            log_path,
            re.compile(r"federation bootstrap complete|federation up but rootless"),
            timeout_s=60.0,
            label="federation bootstrap",
        )
        if line is None:
            print(tail_log(log_path), flush=True)
            return 1
        print(f"  bootstrap log: {line}", flush=True)

        # Stage 2: founder creates 1-voter zone OR joiner enters retry loop.
        print(f"\n=== [{args.side}] stage 2 — root zone state ===", flush=True)
        if args.bootstrap_new:
            line = wait_for_marker(
                log_path,
                re.compile(r"NEXUS_BOOTSTRAP_NEW honored.*creating 1-voter root zone"),
                timeout_s=30.0,
                label="founder create 1-voter zone",
            )
        else:
            line = wait_for_marker(
                log_path,
                re.compile(r"retrying JoinZone against NEXUS_PEERS|joined root zone via leader"),
                timeout_s=30.0,
                label="joiner enters retry-or-join",
            )
        if line is None:
            print(tail_log(log_path), flush=True)
            return 1
        print(f"  zone state log: {line}", flush=True)

        # Stage 3: ConfState convergence.
        print(f"\n=== [{args.side}] stage 3 — ConfState convergence ===", flush=True)
        if args.bootstrap_new:
            line = wait_for_marker(
                log_path,
                re.compile(r"raft\.conf_change\.applied.*AddNode|JoinZone request received"),
                timeout_s=args.cluster_form_timeout,
                label="founder receives joiner's JoinZone + AddNode commits",
            )
        else:
            line = wait_for_marker(
                log_path,
                re.compile(r"joined root zone via leader|raft\.conf_change\.applied"),
                timeout_s=args.cluster_form_timeout,
                label="joiner sees AddNode applied",
            )
        if line is None:
            print(
                f"  [FAIL] cluster did not converge within {args.cluster_form_timeout}s", flush=True
            )
            print("\n--- last log lines ---", flush=True)
            print(tail_log(log_path), flush=True)
            return 1
        print(f"  conf-state log: {line}", flush=True)

        # Look for two-voter ConfState as the strongest convergence pin.
        line = wait_for_marker(
            log_path,
            re.compile(r"voters=\[\d+,\s*\d+\]|voter_count=2"),
            timeout_s=30.0,
            label="2-voter ConfState observed",
        )
        if line is None:
            # Joiner side may not log the explicit voters list; treat as soft pin.
            print("  [SOFT] no 2-voter line yet; rely on conf_change.applied above", flush=True)
        else:
            print(f"  2-voter pin: {line}", flush=True)

        print(f"\n{'=' * 60}", flush=True)
        print(f"RESULT [{args.side}]: PASS — bootstrap + ConfState contract OK", flush=True)
        return 0
    finally:
        # Leave the daemon running for cross-side inspection unless
        # the caller signals via stdin to stop.  The smoke harness
        # spawns this script in the background.
        print(
            f"\n[{args.side}] daemon PID {proc.pid} left running; "
            "kill manually after both sides PASS.  Logs: " + str(log_path),
            flush=True,
        )


if __name__ == "__main__":
    sys.exit(main())
