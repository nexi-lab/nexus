from __future__ import annotations

import os
import signal
import subprocess
import sys

import pytest


def _assert_script_succeeds(code: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr


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
    _assert_script_succeeds(code)


@pytest.mark.skipif(not hasattr(signal, "SIGPIPE"), reason="Unix pipe signals")
def test_embedded_cli_import_keeps_a_closed_pipe_from_killing_the_host_process():
    code = """
import importlib, os
importlib.import_module('nexus.cli.main')
reader, writer = os.pipe()
os.close(reader)
try:
    os.write(writer, b'closed pipe')
except BrokenPipeError:
    pass
else:
    raise AssertionError('the pipe must be closed')
finally:
    os.close(writer)
"""
    _assert_script_succeeds(code)
