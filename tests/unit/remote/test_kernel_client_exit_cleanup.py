"""KernelClient reaps its spawned kernel at interpreter exit (#4777 follow-up).

Every ``nexus`` CLI invocation (including the remote profile's ephemeral
routing kernel) spawns a ``nexus-cluster`` subprocess; nothing terminated it
when the CLI exited, so each run leaked a ~30-thread orphan.
"""

from __future__ import annotations

import atexit
import subprocess
import sys
from typing import Any

import pytest

from nexus.remote import kernel_client as kc


def _sleeper() -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def test_terminate_spawned_kernel_kills_child_and_is_idempotent() -> None:
    client = kc.KernelClient.__new__(kc.KernelClient)
    client._process = _sleeper()
    pid_proc = client._process
    assert pid_proc.poll() is None

    client._terminate_spawned_kernel()
    assert pid_proc.poll() is not None, "child must be reaped"
    assert client._process is None
    # Second call is a no-op.
    client._terminate_spawned_kernel()


def test_terminate_is_noop_without_process() -> None:
    client = kc.KernelClient.__new__(kc.KernelClient)
    client._process = None
    client._terminate_spawned_kernel()
    assert client._process is None


def test_spawn_registers_atexit_hook(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    registered: list[Any] = []
    monkeypatch.setattr(kc.atexit, "register", lambda fn, *a, **k: registered.append(fn))
    monkeypatch.setattr(kc, "_resolve_kernel_binary", lambda: sys.executable)

    class _FakePopen:
        pid = 4242

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.args = args

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(kc.subprocess, "Popen", _FakePopen)

    client = kc.KernelClient(metadata_path=str(tmp_path))
    client._spawn_kernel()

    assert registered == [client._terminate_spawned_kernel]
    # close() must unregister the hook so the object can be collected.
    unregistered: list[Any] = []
    monkeypatch.setattr(kc.atexit, "unregister", lambda fn: unregistered.append(fn))
    client.close()
    assert unregistered == [client._terminate_spawned_kernel]


def test_close_reaps_child_via_same_path() -> None:
    client = kc.KernelClient.__new__(kc.KernelClient)
    client._process = _sleeper()
    client._transport = None
    client._stderr_file = None
    client._stderr_path = None
    client._ephemeral_dir = None
    proc = client._process
    atexit.register(client._terminate_spawned_kernel)  # mirror _spawn_kernel
    client.close()
    assert proc.poll() is not None
    assert client._process is None


# ── Signal death: atexit never runs, the kernel must still go ─────────────


def test_die_with_parent_wraps_a_linux_main_thread_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    monkeypatch.setattr(kc.sys, "platform", "linux")
    argv = kc._die_with_parent_argv([sys.executable, "--flag"])
    assert argv[:5] == [sys.executable, "-I", "-S", "-c", kc._PDEATHSIG_EXEC]
    assert argv[5] == str(os.getpid()), "the wrapper checks it was not orphaned already"
    assert argv[6:] == [sys.executable, "--flag"]


def test_die_with_parent_is_linux_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kc.sys, "platform", "darwin")
    assert kc._die_with_parent_argv([sys.executable]) == [sys.executable]


def test_die_with_parent_skips_unresolvable_binaries(monkeypatch: pytest.MonkeyPatch) -> None:
    # execv does no PATH lookup — leave it to Popen's usual FileNotFoundError.
    monkeypatch.setattr(kc.sys, "platform", "linux")
    assert kc._die_with_parent_argv(["no-such-kernel-binary"]) == ["no-such-kernel-binary"]


def test_die_with_parent_is_not_armed_off_the_main_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    # PDEATHSIG fires when the SPAWNING THREAD exits — a worker thread's
    # spawn would kill the kernel as soon as that worker finished.
    import threading

    monkeypatch.setattr(kc.sys, "platform", "linux")
    argvs: list[list[str]] = []
    worker = threading.Thread(
        target=lambda: argvs.append(kc._die_with_parent_argv([sys.executable]))
    )
    worker.start()
    worker.join()
    assert argvs == [[sys.executable]]


# The spawner runs a live gRPC poller thread — the CLI's real shape, and what
# made a preexec_fn-based arm abort the child (gRPC atfork handlers).
_SIGKILLED_PARENT = r"""
import subprocess, sys, os, signal
sys.path.insert(0, {src!r})
import grpc
channel = grpc.insecure_channel("127.0.0.1:1")
grpc.channel_ready_future(channel)  # starts gRPC's background threads
from nexus.remote.kernel_client import _die_with_parent_argv
child = subprocess.Popen(_die_with_parent_argv(["sleep", "60"]))
print(child.pid, flush=True)
import time; time.sleep(0.5)  # let the wrapper exec
os.kill(os.getpid(), signal.SIGKILL)  # no atexit, like a SIGPIPE'd CLI
"""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux-only")
def test_child_dies_when_its_parent_is_killed() -> None:
    import time
    from pathlib import Path

    src = str(Path(kc.__file__).resolve().parents[2])
    parent = subprocess.run(
        [sys.executable, "-c", _SIGKILLED_PARENT.format(src=src)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert parent.returncode == -9, parent.stderr
    child_pid = int(parent.stdout.strip())
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            # A reparented child can linger as a zombie until init reaps it;
            # kill(pid, 0) still succeeds on a zombie, so read its state.
            stat = Path(f"/proc/{child_pid}/stat").read_text()
        except (FileNotFoundError, ProcessLookupError):
            return  # gone; ESRCH when reaped between open and read
        if stat.rsplit(")", 1)[1].split()[0] == "Z":
            return
        time.sleep(0.1)
    pytest.fail(f"child {child_pid} outlived its SIGKILLed parent")
