"""Co-host A2A duet E2E — a REAL sudocode LLM agent conversing over the nexus
A2A mailbox, hosted in a ``nexusd-cluster-cohost`` container.

This is the Docker counterpart of the in-process unit proof
(sudocode ``tools/tests/cohost_live_llm.rs``): it drives the SAME
``spawn_managed_agent`` factory, but through a real gRPC daemon over the wire,
so it also exercises the control-plane ``Call`` path and the A2A mailbox
replication substrate.

Flow (see ``dockerfiles/docker-compose.cohost-duet.yml``):

1. ``Call managed_agent.start_session_v1`` spawns the co-host agent
   ``mac-ai`` — ``SudoCodeSpawnAdapter.spawn`` → ``spawn_managed_agent``, whose
   poller discovers peers by listing ``/agents/mac-ai/conversations/``.
2. We provision the conversation the way ``Mailbox::ensure_conversation`` does
   — a chat-list entry for BOTH sides plus the transcript stream — and seed a
   ``MailboxEnvelope`` ``{from: win-ai, to: mac-ai, body: …}`` into it.
3. The agent tails it, runs a REAL LLM turn (funded sudorouter key), and calls
   ``send_message`` to reply. A conversation is ONE log for both directions, so
   the reply lands in the SAME transcript — asserted there, carrying a ``from``
   the kernel stamped.

Gated: skips unless the ``docker-compose.cohost-duet`` stack is up (the compose
sets ``COHOST_DUET_E2E=1``) AND a funded ``SUDOROUTER_API_KEY`` is available —
the LLM turn 403s on a balance-capped key.
"""

from __future__ import annotations

import json
import os
import time

import blake3
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


# Mirrors `a2a::conversation_id`: blake3 over the SORTED pair, NUL-separated,
# truncated to 32 hex chars. Sorting is what makes it order-free, so both sides
# derive the same id without coordinating; the NUL is why ("a-b","c") and
# ("a","b-c") cannot collide. Agent names are path segments (ASCII), so Python's
# code-point ordering and Rust's byte ordering agree here.
def _conversation_id(a: str, b: str) -> str:
    first, second = (a, b) if a <= b else (b, a)
    h = blake3.blake3()
    h.update(first.encode())
    h.update(b"\x00")
    h.update(second.encode())
    return h.hexdigest()[:32]


# Cross-language pin, asserted from the other side in nexus-vfs
# `a2a::addresses::conversation_id_matches_the_cross_language_literal`. Two
# implementations of one derivation drift silently: this test would provision a
# conversation the agent never looks at and fail as "no reply", pointing at the
# LLM or the poller rather than at the id.
_CID_PIN = ("mac-ai", "win-ai", "211de372cab12e723fee2c1426ca0651")


def _conversation_root(a: str, b: str) -> str:
    return f"/conversations/{_conversation_id(a, b)}"


def _transcript(a: str, b: str) -> str:
    return f"{_conversation_root(a, b)}/transcript"


def _chat_list_entry(owner: str, peer: str) -> str:
    return f"/agents/{owner}/conversations/{peer}"


def _provision_conversation(a: str, b: str) -> str:
    """Create the conversation between `a` and `b`, as its participants would.

    Mirrors ``Mailbox::ensure_conversation``. The chat-list entry is written for
    BOTH sides because a receiver finds its peers by listing that directory — an
    entry only on the sender's side is a message nobody is listening for. Each
    entry is a plain write holding the conversation root, not a link: the gRPC
    surface carries no link target, so a link would silently not appear.
    """
    root = _conversation_root(a, b)
    for owner, other in ((a, b), (b, a)):
        rh.vfs_mkdir(GRPC, f"/agents/{owner}/conversations", api_key=API_KEY)
        wrote = rh.vfs_write(GRPC, _chat_list_entry(owner, other), root.encode(), api_key=API_KEY)
        assert "error" not in wrote, f"chat-list entry for {owner} failed: {wrote}"

    created = rh.vfs_create_stream(
        GRPC,
        _transcript(a, b),
        io_profile="wal,memory",
        capacity=65_536,
        api_key=API_KEY,
    )
    assert "error" not in created, f"transcript create failed: {created}"
    return root


def _frames(blob: bytes) -> list[dict]:
    """Split concatenated JSON envelopes into objects.

    ``StreamCollectAll`` returns the payloads back to back with no separator, so
    the decoder is what finds the boundaries.
    """
    out: list[dict] = []
    text = blob.decode("utf-8", "replace").strip()
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(text):
        try:
            obj, end = decoder.raw_decode(text, idx)
        except ValueError:
            break
        out.append(obj)
        idx = end
        while idx < len(text) and text[idx] in " \r\n\t":
            idx += 1
    return out


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
    a, b, expected = _CID_PIN
    assert _conversation_id(a, b) == expected, (
        "conversation id derivation has drifted from the substrate; "
        "see a2a::addresses::conversation_id_matches_the_cross_language_literal"
    )

    _wait_daemon_ready()

    # 1. Spawn the responder via the control plane. Its poller then discovers
    #    peers by listing /agents/mac-ai/conversations/.
    started = rh.grpc_call(
        GRPC,
        "managed_agent.start_session_v1",
        {"agent_id": RESPONDER, "model": MODEL, "owner_id": "root", "zone_id": "root"},
        api_key=API_KEY,
        timeout=30,
    )
    assert "error" not in started, f"start_session failed: {started}"
    assert started.get("result", {}).get("session_id"), f"no session_id: {started}"

    # 2. Provision the conversation BEFORE the poller's first discovery pass,
    #    so there is a peer to find. Creating it later is not wrong — the poller
    #    re-lists — it just costs a discovery interval.
    transcript = _transcript(SEEDER, RESPONDER)
    _provision_conversation(SEEDER, RESPONDER)
    time.sleep(2)

    # 3. Seed one message from win-ai into the shared transcript.
    envelope = json.dumps(
        {
            "from": SEEDER,
            "to": RESPONDER,
            "body": (
                "You are being tested over a nexus A2A mailbox. Reply with exactly one word: PONG"
            ),
        }
    ).encode()
    seeded = rh.vfs_stream_write(GRPC, transcript, envelope, api_key=API_KEY)
    assert "error" not in seeded, f"seed append failed: {seeded}"

    # 4. The agent tails it, runs a real LLM turn, and send_message-replies into
    #    the same conversation. Poll the transcript for a frame that is NOT our
    #    seed — one log carries both directions, so the sender is what tells
    #    them apart.
    deadline = time.time() + REPLY_TIMEOUT_S
    reply = None
    while time.time() < deadline and reply is None:
        collected = rh.stream_collect_all(GRPC, transcript, api_key=API_KEY)
        if "error" not in collected:
            for frame in _frames(collected["result"]["data"]):
                if frame.get("from") == RESPONDER:
                    reply = frame
                    break
        if reply is None:
            time.sleep(2)

    assert reply, f"{RESPONDER} never replied in {transcript} within {REPLY_TIMEOUT_S}s"
    # `from` is stamped by the kernel, not by the agent, so asserting it is
    # asserting the identity guarantee and not just the routing.
    assert "PONG" in reply.get("body", "").upper(), f"unexpected LLM reply: {reply}"
