"""Co-host A2A duet E2E — a REAL sudocode LLM agent conversing over the nexus
A2A mailbox, hosted in a ``nexusd-cluster-cohost`` container.

This is the Docker counterpart of the in-process unit proof
(sudocode ``tools/tests/cohost_live_llm.rs``): it drives the SAME
``spawn_managed_agent`` factory, but through a real gRPC daemon over the wire,
so it also exercises the control-plane ``Call`` path and the A2A mailbox
replication substrate.

Flow (see ``dockerfiles/docker-compose.cohost-duet.yml``):

1. ``Call managed_agent.start_session_v1`` spawns the co-host agent
   ``mac-ai`` — ``SudoCodeSpawnAdapter.spawn`` → ``spawn_managed_agent`` binds
   it to its replicated A2A inbox ``/agents/mac-ai/chat-with-me``.
2. We seed a ``MailboxEnvelope`` ``{from: win-ai, to: mac-ai, body: …}`` there.
3. The agent reads it, runs a REAL LLM turn (funded sudorouter key), and calls
   ``send_message`` to reply into the SENDER's inbox
   ``/agents/win-ai/chat-with-me`` — where we assert the reply lands.

Gated: skips unless the ``docker-compose.cohost-duet`` stack is up (the compose
sets ``COHOST_DUET_E2E=1``) AND a funded ``SUDOROUTER_API_KEY`` is available —
the LLM turn 403s on a balance-capped key.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from tests.e2e.docker import runbook_helpers as rh

# The compose's `test` service (CI) sets COHOST_DUET_E2E=1; local runs export it.
_STACK_UP = os.environ.get("COHOST_DUET_E2E") == "1"
_HAS_KEY = bool(os.environ.get("SUDOROUTER_API_KEY"))

pytestmark = pytest.mark.skipif(
    not (_STACK_UP and _HAS_KEY),
    reason=(
        "cohost-duet E2E needs the docker-compose.cohost-duet stack up "
        "(COHOST_DUET_E2E=1) and a funded SUDOROUTER_API_KEY (the LLM turn "
        "403s on a balance-capped key)"
    ),
)

# gRPC endpoint of the cohost daemon (compose maps 2126 to the host).
GRPC = os.environ.get("COHOST_DUET_GRPC", "localhost:2126")
# Auth-off daemon (`--insecure-no-auth`); the client key is ignored.
API_KEY = getattr(rh, "ADMIN_API_KEY", "")

RESPONDER = "mac-ai"
SEEDER = "win-ai"
MODEL = os.environ.get("COHOST_DUET_MODEL", "claude-sonnet-4-6")

# Budget for the whole turn: LLM latency + a mailbox round-trip. Sonnet PONGs
# in ~4s locally; 120s is generous headroom for a loaded CI runner.
REPLY_TIMEOUT_S = 120


def _inbox(agent: str) -> str:
    return f"/agents/{agent}/chat-with-me"


def _wait_daemon_ready(timeout_s: int = 60) -> None:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        stat = rh.vfs_stat(GRPC, "/", api_key=API_KEY, timeout=5)
        if isinstance(stat, dict) and "error" not in stat:
            return
        last = stat
        time.sleep(1)
    pytest.fail(f"cohost daemon on {GRPC} never became reachable in {timeout_s}s (last={last})")


def _decode(read_result: object) -> str:
    out = rh.decode_content(read_result) if hasattr(rh, "decode_content") else read_result
    if isinstance(out, (bytes, bytearray)):
        return out.decode("utf-8", "replace")
    return out if isinstance(out, str) else str(out)


def test_cohost_agent_replies_over_a2a_mailbox() -> None:
    """A daemon-hosted co-host agent LLM-replies to a peer over the A2A mailbox."""
    _wait_daemon_ready()

    # 1. Spawn the responder via the control plane. The adapter binds it to its
    #    replicated A2A inbox /agents/mac-ai/chat-with-me.
    started = rh.grpc_call(
        GRPC,
        "managed_agent.start_session_v1",
        {"agent_id": RESPONDER, "model": MODEL, "owner_id": "root", "zone_id": "root"},
        api_key=API_KEY,
        timeout=30,
    )
    assert "error" not in started, f"start_session failed: {started}"
    assert started.get("result", {}).get("session_id"), f"no session_id: {started}"

    # Give the loop a beat to arm its sys_watch on the inbox.
    time.sleep(2)

    # 2. Seed one message from win-ai into mac-ai's inbox.
    envelope = json.dumps(
        {
            "from": SEEDER,
            "to": RESPONDER,
            "body": (
                "You are being tested over a nexus A2A mailbox. "
                "Reply with exactly one word: PONG"
            ),
        }
    ).encode()
    seeded = rh.vfs_write(GRPC, _inbox(RESPONDER), envelope, api_key=API_KEY)
    assert "error" not in seeded, f"seed write failed: {seeded}"

    # 3. The agent reads it, runs a real LLM turn, and send_message-replies into
    #    the SEEDER's inbox. Poll for the reply.
    deadline = time.time() + REPLY_TIMEOUT_S
    reply_text = ""
    while time.time() < deadline:
        read = rh.vfs_read(GRPC, _inbox(SEEDER), api_key=API_KEY)
        if not (isinstance(read, dict) and "error" in read):
            body = _decode(read).strip()
            if body:
                reply_text = body
                break
        time.sleep(2)

    assert reply_text, (
        f"{RESPONDER} never replied into {_inbox(SEEDER)} within {REPLY_TIMEOUT_S}s"
    )
    # The reply is a MailboxEnvelope stamped from the responder, carrying its
    # LLM output. Assert both the routing (from == responder) and the content.
    parsed = json.loads(reply_text)
    assert parsed.get("from") == RESPONDER, f"reply not from {RESPONDER}: {parsed}"
    assert "PONG" in parsed.get("body", "").upper(), f"unexpected LLM reply: {parsed}"
