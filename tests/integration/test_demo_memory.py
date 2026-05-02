"""Demo profile cold-start memory regression (Issue #3997).

Boots the nexus-stack compose, waits for readiness, then reads
/proc/$pid/status from inside the container and asserts that idle
RSS, VmData, and thread count are within the targets agreed in the
issue:

    VmRSS  <= 450 MB
    VmData <= 4 GB
    Threads <= 20

Skipped by default. Run with:
    uv run pytest -m demo_memory tests/integration/test_demo_memory.py -v
"""

import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_COMPOSE = REPO_ROOT / "nexus-stack.yml"
PROJECT_PREFIX = "nexus-demo-mem"
SERVICE = "nexus"
READINESS_TIMEOUT = 90  # seconds — image build + DB init + service start
LAZY_INIT_BUFFER = 30  # seconds (matches issue reproduction)
RSS_LIMIT_KB = 450 * 1024
VMDATA_LIMIT_KB = 4 * 1024 * 1024
THREADS_LIMIT = 20


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _parse_proc_status(blob: str) -> dict[str, int]:
    """Parse /proc/$pid/status into {VmRSS_kB, VmData_kB, Threads}."""
    out: dict[str, int] = {}
    for line in blob.splitlines():
        if line.startswith("VmRSS:"):
            out["VmRSS_kB"] = int(line.split()[1])
        elif line.startswith("VmData:"):
            out["VmData_kB"] = int(line.split()[1])
        elif line.startswith("Threads:"):
            out["Threads"] = int(line.split()[1])
    missing = {"VmRSS_kB", "VmData_kB", "Threads"} - out.keys()
    assert not missing, f"could not parse {missing} from /proc/$pid/status"
    return out


@pytest.mark.demo_memory
@pytest.mark.skipif(not _docker_available(), reason="docker CLI not available")
def test_demo_idle_rss_under_limit():
    project = f"{PROJECT_PREFIX}-{uuid.uuid4().hex[:8]}"
    container = f"{project}-{SERVICE}-1"
    env = os.environ.copy()

    try:
        subprocess.check_call(
            ["docker", "compose", "-p", project, "-f", str(DEMO_COMPOSE), "up", "-d"],
            env=env,
            cwd=str(REPO_ROOT),
        )

        # Poll readiness up to READINESS_TIMEOUT seconds via container's healthz.
        deadline = time.time() + READINESS_TIMEOUT
        ready = False
        while time.time() < deadline:
            rc = subprocess.call(
                [
                    "docker",
                    "exec",
                    container,
                    "curl",
                    "-fsS",
                    "http://localhost:2026/healthz/ready",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if rc == 0:
                ready = True
                break
            time.sleep(2)
        assert ready, f"{container} did not become ready within {READINESS_TIMEOUT}s"

        # Lazy bg init (skeleton indexer, search daemon hooks, etc.)
        time.sleep(LAZY_INIT_BUFFER)

        status = subprocess.check_output(
            [
                "docker",
                "exec",
                container,
                "sh",
                "-c",
                "pid=$(grep -l nexusd /proc/[0-9]*/comm 2>/dev/null | head -1 | cut -d/ -f3); cat /proc/$pid/status",
            ]
        ).decode()
        metrics = _parse_proc_status(status)

        def _diag() -> str:
            try:
                maps = subprocess.check_output(
                    [
                        "docker",
                        "exec",
                        container,
                        "sh",
                        "-c",
                        "pid=$(grep -l nexusd /proc/[0-9]*/comm 2>/dev/null | head -1 | cut -d/ -f3); head -20 /proc/$pid/maps",
                    ]
                ).decode()
            except Exception as e:
                maps = f"<diag failed: {e}>"
            return f"\n--- /proc/$pid/status ---\n{status}\n--- /proc/$pid/maps (head) ---\n{maps}"

        assert metrics["VmRSS_kB"] < RSS_LIMIT_KB, (
            f"VmRSS={metrics['VmRSS_kB']} kB exceeds {RSS_LIMIT_KB} kB" + _diag()
        )
        assert metrics["Threads"] < THREADS_LIMIT, (
            f"Threads={metrics['Threads']} exceeds {THREADS_LIMIT}" + _diag()
        )
        assert metrics["VmData_kB"] < VMDATA_LIMIT_KB, (
            f"VmData={metrics['VmData_kB']} kB exceeds {VMDATA_LIMIT_KB} kB" + _diag()
        )
    finally:
        subprocess.call(
            [
                "docker",
                "compose",
                "-p",
                project,
                "-f",
                str(DEMO_COMPOSE),
                "down",
                "-v",
            ],
            env=env,
            cwd=str(REPO_ROOT),
        )
