"""JSON-lines IPC protocol for agent worker subprocess communication.

Defines the wire format between nexusd (ProcessManager) and the worker
subprocess (worker.py).  Each message is a single JSON object terminated
by ``\\n`` — the same JSON-lines convention used by ACP (PR #3060) and
task dispatch (PR #3059).

Transport: subprocess stdin (nexusd → worker) and stdout (worker → nexusd).
Both sides see the same VFS fd/0 (stdin) and fd/1 (stdout) DT_STREAMs.

Philosophy: **everything is a file**.  The worker is a user-space process
that makes "syscalls" to nexusd for all I/O:
  - LLM inference → ``llm_request`` / ``llm_response`` (VFS SudoRouter)
  - Tool dispatch → ``tool_calls`` / ``tool_results``  (VFS operations)
  - State save    → ``checkpoint`` / ``turn_complete``  (VFS session store)

Direction: nexusd → worker (stdin / fd/0)
    init           — once at spawn: config, tools
    user_message   — each send(): user message + system prompt + history
    llm_response   — inner loop: LLM inference result (content, tool_calls)
    tool_results   — inner loop: dispatched tool call results
    cancel         — terminate(): request graceful exit

Direction: worker → nexusd (stdout / fd/1)
    ready          — after init processed, worker is alive
    llm_request    — inner loop: request LLM inference (messages, tools)
    tool_calls     — inner loop: tool calls for nexusd to dispatch
    checkpoint     — after each tool round: save conversation state
    turn_complete  — inner loop done: final messages + back to SLEEPING
    error          — unrecoverable error
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

# ---------------------------------------------------------------------------
# Message type constants
# ---------------------------------------------------------------------------

# nexusd → worker (stdin / fd/0)
MSG_INIT = "init"
MSG_USER_MESSAGE = "user_message"
MSG_LLM_RESPONSE = "llm_response"
MSG_TOOL_RESULTS = "tool_results"
MSG_CANCEL = "cancel"

# worker → nexusd (stdout / fd/1)
MSG_READY = "ready"
MSG_LLM_REQUEST = "llm_request"
MSG_TOOL_CALLS = "tool_calls"
MSG_CHECKPOINT = "checkpoint"
MSG_TURN_COMPLETE = "turn_complete"
MSG_ERROR = "error"


# ---------------------------------------------------------------------------
# Codec — JSON-lines (one JSON object per \n-terminated line)
# ---------------------------------------------------------------------------


def encode(msg: dict[str, Any]) -> bytes:
    """Encode a message dict to a JSON-line (compact, newline-terminated)."""
    return json.dumps(msg, separators=(",", ":")).encode() + b"\n"


def decode(line: bytes) -> dict[str, Any]:
    """Decode a JSON-line back to a message dict."""
    result: dict[str, Any] = json.loads(line.strip())
    return result


# ---------------------------------------------------------------------------
# Async I/O helpers — read/write on asyncio streams
# ---------------------------------------------------------------------------


async def read_message(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    """Read one JSON-line message from an async stream.

    Returns None on EOF (subprocess exited or stdin closed).
    """
    line = await reader.readline()
    if not line:
        return None
    return decode(line)


def write_message(writer: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
    """Write one JSON-line message to an async stream (non-draining).

    Caller must ``await writer.drain()`` when appropriate.
    """
    writer.write(encode(msg))
