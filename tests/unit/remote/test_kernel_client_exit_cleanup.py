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
