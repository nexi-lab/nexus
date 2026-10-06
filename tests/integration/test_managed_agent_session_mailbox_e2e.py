"""A real managed subprocess exchanges ACP objects over its conversation log.

Exercises generic Call, authenticated StreamWriteNowait and StreamReadAt on a
booted daemon. Stdio is internal to the subprocess adapter; no public fd streams.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from nexus.remote.kernel_client import KernelClient
from tests.helpers.session_mailbox import SessionMailboxCodec

_KERNEL_BIN = os.environ.get("NEXUS_KERNEL_BINARY")

_PROBE = "hello-session-mailbox"
_ROUNDTRIP_TIMEOUT_S = 15.0


def _open_kernel(data_dir: Path) -> KernelClient:
    client = KernelClient(metadata_path=str(data_dir))
    try:
        client.open()
    except Exception:  # noqa: BLE001 — boot failure ⇒ skip unless pinned
        client.close()
        if _KERNEL_BIN:
            raise
        pytest.skip(
            "nexusd-cluster binary unavailable; set NEXUS_KERNEL_BINARY to "
            "the nexus-local nexusd-cluster (it hosts managed_agent) to run "
            "this E2E."
        )
    return client


def test_managed_agent_session_mailbox_roundtrip(tmp_path: Path) -> None:
    client = _open_kernel(tmp_path)
    try:
        script = (
            "import json,sys; m=json.loads(sys.stdin.readline()); "
            "print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':m['params']}),flush=True)"
        )
        started = client._call(
            "managed_agent.start_session_v1",
            {
                "agent_id": "e2e-mailbox",
                "spawn_spec": {
                    "cmd": sys.executable,
                    "args": ["-u", "-c", script],
                    "env": dict(os.environ),
                    "cwd": str(tmp_path),
                },
            },
        )
        assert started.get("os_pid"), started
        codec = SessionMailboxCodec(started["session_endpoint"])
        path = codec.endpoint["transcript"]
        assert path.startswith("/conversations/")
        client.stream_write_nowait(
            path,
            codec.encode(
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "probe",
                    "params": {"text": _PROBE},
                }
            ),
        )
        deadline = time.monotonic() + _ROUNDTRIP_TIMEOUT_S
        offset = 0
        reply = None
        closed = None
        while time.monotonic() < deadline and closed is None:
            record = client.stream_read_at(path, offset)
            if not record or not record["data"]:
                time.sleep(0.05)
                continue
            offset = record["next_offset"]
            frame = codec.decode(json.loads(bytes(record["data"])))
            if not frame:
                continue
            if frame["type"] == "rpc" and frame["message"].get("id") == 0:
                reply = frame["message"]
            elif frame["type"] == "closed":
                closed = frame
        assert reply == {"jsonrpc": "2.0", "id": 0, "result": {"text": _PROBE}}
        assert closed is not None, "subprocess exit must close the same mailbox channel"
    finally:
        client.close()
