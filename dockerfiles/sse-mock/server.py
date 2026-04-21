"""SSE mock sidecar for LLM streaming E2E tests.

Serves OpenAI- and Anthropic-shaped ``text/event-stream`` responses so the
Rust ``OpenAIBackend`` / ``AnthropicBackend`` can be exercised end-to-end
without hitting a real provider. Emits a short, deterministic completion
for every request — CAS storage verification just needs the stream to
complete and the aggregated text to match a known value.

Protocol fidelity is minimal on purpose: the Rust streaming parsers are
unit-tested against full provider fixtures; this mock only needs to
hand back a well-formed event stream the parser can consume.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from aiohttp import web

HELLO_WORLD = "Hello from SSE mock."


async def _openai_events() -> AsyncIterator[bytes]:
    """Yield OpenAI /v1/chat/completions SSE chunks."""
    # Split the canned reply into tokens so the consumer sees multiple deltas.
    parts = HELLO_WORLD.split()
    completion_id = "cmpl-mock-0001"
    created = 1_700_000_000
    model = "mock-gpt-4o-mini"

    for i, token in enumerate(parts):
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "content": (" " if i > 0 else "") + token,
                    },
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(chunk)}\n\n".encode()

    final = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }
        ],
    }
    yield f"data: {json.dumps(final)}\n\n".encode()
    yield b"data: [DONE]\n\n"


async def _anthropic_events() -> AsyncIterator[bytes]:
    """Yield Anthropic /v1/messages SSE chunks (message_start/delta/stop)."""
    message_id = "msg_mock_0001"
    model = "mock-claude-3-5-sonnet"
    parts = HELLO_WORLD.split()

    start = {
        "type": "message_start",
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 0},
        },
    }
    yield f"event: message_start\ndata: {json.dumps(start)}\n\n".encode()

    cb_start = {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    }
    yield f"event: content_block_start\ndata: {json.dumps(cb_start)}\n\n".encode()

    for i, token in enumerate(parts):
        delta = {
            "type": "content_block_delta",
            "index": 0,
            "delta": {
                "type": "text_delta",
                "text": (" " if i > 0 else "") + token,
            },
        }
        yield f"event: content_block_delta\ndata: {json.dumps(delta)}\n\n".encode()

    cb_stop = {"type": "content_block_stop", "index": 0}
    yield f"event: content_block_stop\ndata: {json.dumps(cb_stop)}\n\n".encode()

    msg_delta = {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": len(parts)},
    }
    yield f"event: message_delta\ndata: {json.dumps(msg_delta)}\n\n".encode()

    yield b'event: message_stop\ndata: {"type":"message_stop"}\n\n'


async def handle_openai(request: web.Request) -> web.StreamResponse:
    await request.json()
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
    await resp.prepare(request)
    async for chunk in _openai_events():
        await resp.write(chunk)
    await resp.write_eof()
    return resp


async def handle_anthropic(request: web.Request) -> web.StreamResponse:
    await request.json()
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
    await resp.prepare(request)
    async for chunk in _anthropic_events():
        await resp.write(chunk)
    await resp.write_eof()
    return resp


async def handle_healthz(_: web.Request) -> web.Response:
    return web.Response(text="ok")


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle_openai)
    app.router.add_post("/v1/messages", handle_anthropic)
    app.router.add_get("/healthz", handle_healthz)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=8080, access_log=None)
