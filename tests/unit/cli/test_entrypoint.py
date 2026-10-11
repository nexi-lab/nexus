from __future__ import annotations

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("worker_thread", [False, True])
def test_import_keeps_the_host_signal_handler_and_event_loop_policy(worker_thread):
    code = f"""
import asyncio, importlib, signal, threading
policy = asyncio.get_event_loop_policy()
def handler(signum, frame): pass
if hasattr(signal, 'SIGPIPE'):
    signal.signal(signal.SIGPIPE, handler)
errors = []
def load():
    try:
        importlib.import_module('nexus.cli.main')
    except BaseException as exc:
        errors.append(repr(exc))
if {worker_thread!r}:
    thread = threading.Thread(target=load, daemon=True)
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive(), 'CLI import hung'
else:
    load()
assert not errors, errors
if hasattr(signal, 'SIGPIPE'):
    assert signal.getsignal(signal.SIGPIPE) is handler
assert asyncio.get_event_loop_policy() is policy
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
