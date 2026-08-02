"""E2E: managed_agent raw ACP control-plane tunnel over gRPC (task #36).

Boots a real ``nexusd-cluster`` daemon (same ``KernelClient`` spawn harness
as the restart-survival smoke) and drives the raw-byte stdio tunnel end to
end over the *actual* wire path an embedder (sudowork's grpc-js client) uses:

  1. ``ManagedAgentService.start_session(spawn_spec)`` via the ``Call`` RPC
     (``managed_agent.start_session_v1``, JSON payload) — spawns a subprocess
     and returns ``{session_id, os_pid}``.
  2. write stdin bytes to the ``/proc/{session_id}/fd/0`` DT_STREAM
     (``StreamWriteNowait``).
  3. read the echoed bytes back off ``/proc/{session_id}/fd/1``
     (``StreamReadAt``, non-blocking poll).

The subprocess is ``sh -c 'read line; printf ...'`` — a line-buffered,
self-terminating echo, so a single line round-trips promptly (``cat`` would
block-buffer its stdout on a pipe and never flush until EOF) and the child
exits on its own, exercising the supervisor reap path too.

This is the gap the in-process ``raw_spawn`` unit tests don't cover: the
gRPC ``Call`` routing to the Rust service + the ``StreamReadAt`` /
``StreamWriteNowait`` typed RPCs, through a booted daemon. Requires the
nexus-local ``nexusd-cluster`` (which brings up ``managed_agent``); the
pure nexus-vfs cluster binary has no such service, so the test skips there.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from nexus.remote.kernel_client import KernelClient

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="raw ACP subprocess host is unix-only (subprocess-host feature)",
)

# The tunnel + managed_agent service live in the nexus-local nexusd-cluster.
# Point the spawn harness at it explicitly; if it (or any cluster binary) is
# missing, skip rather than fail — matches the restart-survival smoke.
_KERNEL_BIN = os.environ.get("NEXUS_KERNEL_BINARY")

_PROBE = b"hello-nexus-tunnel\n"
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


def test_managed_agent_tunnel_roundtrip(tmp_path: Path) -> None:
    """spawn_spec → real subprocess → bidi raw-byte tunnel round-trips.

    Calls start_session directly with no client-side boot-window retry: the
    daemon holds a service ``Call`` that arrives before ``bring_up_services``
    has enlisted the services (nexus-vfs `Call` readiness gate), so a
    spawn-then-immediately-connect client no longer needs to poll for the
    control plane to appear.
    """
    client = _open_kernel(tmp_path)
    try:
        resp = client._call(
            "managed_agent.start_session_v1",
            {
                "agent_id": "e2e-tunnel",
                # Line-buffered self-terminating echo: reads one line from the
                # stdin stream, writes it (+newline) to stdout, exits.
                "spawn_spec": {
                    "cmd": "sh",
                    "args": ["-c", 'read line; printf "%s\\n" "$line"'],
                    "env": {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                    "cwd": "/tmp",
                },
            },
        )
        session_id = resp["session_id"]
        assert session_id, f"no session_id in start_session response: {resp!r}"
        # os_pid is set ONLY on the raw control-plane (spawn_spec) path —
        # its presence proves the RawSpawn provider (subprocess-host) is wired
        # into the shipped daemon, not just the mailbox/workspace core.
        assert resp.get("os_pid"), f"raw spawn must return a real os_pid: {resp!r}"

        fd_stdin = f"/proc/{session_id}/fd/0"
        fd_stdout = f"/proc/{session_id}/fd/1"

        client.stream_write_nowait(fd_stdin, _PROBE)

        # Poll fd/1 non-blocking until the echo comes back. `None` == no data
        # yet (stream still open) — keep polling; the child flushes + exits
        # after echoing, which drains the tail and closes the stream.
        got = b""
        offset = 0
        deadline = time.monotonic() + _ROUNDTRIP_TIMEOUT_S
        while time.monotonic() < deadline and got != _PROBE:
            chunk = client.stream_read_at(fd_stdout, offset)
            if chunk and chunk["data"]:
                got += bytes(chunk["data"])
                offset = chunk["next_offset"]
            else:
                time.sleep(0.05)

        assert got == _PROBE, (
            f"tunnel round-trip mismatch: wrote {_PROBE!r}, read back {got!r} "
            f"(session_id={session_id})"
        )
    finally:
        client.close()
