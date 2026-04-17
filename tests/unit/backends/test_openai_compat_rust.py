"""Integration test for the Rust OpenAI + Anthropic LLM backends (R10d.8 + PR-B A.6).

Exercises the end-to-end path: `NexusFS.sys_setattr(backend_type=...)` wires
the Rust backend, `nx.llm_start_streaming(...)` runs the full SSE → DT_STREAM
→ CAS-persist pipeline, and `nx.cas_read(session_hash)` retrieves the
persisted session envelope.

SSE is served by a single-shot `http.server`-based fixture running on
127.0.0.1 — no pytest-httpserver dependency.
"""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from typing import Any

import pytest

from nexus.backends.storage.cas_local import CASLocalBackend
from nexus.contracts.metadata import DT_MOUNT
from nexus.core.config import ParseConfig, PermissionConfig
from nexus.factory import create_nexus_fs
from nexus.storage.record_store import SQLAlchemyRecordStore
from tests.helpers.dict_metastore import DictMetastore


def _one_shot_sse_server(body: str) -> tuple[str, threading.Thread]:
    """Bind to 127.0.0.1:0 and answer one HTTP request with the given SSE body.

    Returns ``(base_url, thread)``. The thread serves exactly one request and
    then exits — sufficient for the round-trip test which makes one POST.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]

    def _serve() -> None:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        try:
            # Drain the request (best-effort). The Rust client sends a single
            # POST /chat/completions (or /v1/messages) with a JSON body.
            conn.settimeout(2.0)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(8192)
                if not chunk:
                    break
                buf += chunk
            # Drain the POST body too so reqwest doesn't reset before seeing
            # our response. Approximate — we read whatever's already queued.
            try:
                while True:
                    more = conn.recv(8192)
                    if not more:
                        break
            except OSError:
                pass

            resp = (
                f"HTTP/1.1 200 OK\r\n"
                f"Content-Type: text/event-stream\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Connection: close\r\n"
                f"\r\n"
                f"{body}"
            ).encode()
            conn.sendall(resp)
        finally:
            try:
                conn.close()
            except Exception:
                pass
            sock.close()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    return f"http://127.0.0.1:{port}", t


def _one_shot_error_server(status: int, body: str) -> tuple[str, threading.Thread]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]

    def _serve() -> None:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        try:
            conn.settimeout(2.0)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(8192)
                if not chunk:
                    break
                buf += chunk
            resp = (
                f"HTTP/1.1 {status} Error\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Connection: close\r\n"
                f"\r\n"
                f"{body}"
            ).encode()
            conn.sendall(resp)
        finally:
            try:
                conn.close()
            except Exception:
                pass
            sock.close()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    return f"http://127.0.0.1:{port}", t


def _sse(frames: list[str]) -> str:
    return "".join(f"{f}\n\n" for f in frames)


def _openai_sse_happy() -> str:
    return _sse(
        [
            'data: {"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"Hel"}}]}',
            'data: {"model":"gpt-4o","choices":[{"index":0,"delta":{"content":"lo"}}]}',
            'data: {"model":"gpt-4o","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
    )


def _anthropic_sse_happy() -> str:
    return _sse(
        [
            'event: message_start\ndata: {"message":{"model":"claude-sonnet-4-20250514","usage":{"input_tokens":3}}}',
            'event: content_block_start\ndata: {"index":0,"content_block":{"type":"text","text":""}}',
            'event: content_block_delta\ndata: {"index":0,"delta":{"type":"text_delta","text":"Hel"}}',
            'event: content_block_delta\ndata: {"index":0,"delta":{"type":"text_delta","text":"lo"}}',
            'event: content_block_stop\ndata: {"index":0}',
            'event: message_delta\ndata: {"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}',
            "event: message_stop\ndata: {}",
        ]
    )


async def _bootstrap(tmp_path: Path) -> Any:
    """Spin up an in-process NexusFS with a root CASLocal backend."""
    return await create_nexus_fs(
        backend=CASLocalBackend(tmp_path / "data"),
        metadata_store=DictMetastore(),
        record_store=SQLAlchemyRecordStore(db_path=tmp_path / "meta.db"),
        parsing=ParseConfig(auto_parse=False),
        permissions=PermissionConfig(enforce=False),
    )


def _collect_stream(nx: Any, stream_path: str, expected_done: bool = True) -> str:
    """Read everything from a DT_STREAM until it closes. Returns decoded UTF-8.

    The Rust backend writes content deltas followed by a final `done` or
    `error` JSON control frame, then closes the stream — ``stream_collect_all``
    blocks until close().
    """
    data = nx.stream_collect_all(stream_path)
    return data.decode("utf-8")


def _run_streaming(nx: Any, mount: str, request: dict[str, Any]) -> tuple[str, str]:
    """Invoke llm_start_streaming synchronously. Returns (payload, stream_path).

    The kernel runs the SSE pump on a worker thread — ``llm_start_streaming``
    blocks until the stream is closed (done frame emitted), which gives us a
    deterministic point to assert on.
    """
    stream_path = f"{mount}/stream/session-0"
    nx._kernel.create_stream(stream_path, 65_536)
    req_bytes = json.dumps(request).encode("utf-8")
    nx._kernel.llm_start_streaming(mount, "root", req_bytes, stream_path)
    return _collect_stream(nx, stream_path), stream_path


class TestRustOpenAIBackendMount:
    """Mount Rust OpenAIBackend via sys_setattr(backend_type="openai")."""

    @pytest.mark.asyncio
    async def test_streaming_round_trip(self, tmp_path: Path) -> None:
        base_url, _t = _one_shot_sse_server(_openai_sse_happy())
        nx = await _bootstrap(tmp_path)
        try:
            nx.sys_setattr(
                "/llm",
                entry_type=DT_MOUNT,
                backend_type="openai",
                backend_name="openai_compatible",
                openai_base_url=base_url,
                openai_api_key="sk-test",
                openai_model="gpt-4o",
                openai_blob_root=str(tmp_path / "llm_spool"),
            )

            payload, _ = _run_streaming(
                nx,
                "/llm",
                {"messages": [{"role": "user", "content": "hi"}], "model": "gpt-4o"},
            )

            # Body: "Hello" + JSON done frame.
            assert payload.startswith("Hello")
            done_idx = payload.index("{")
            done = json.loads(payload[done_idx:])
            assert done["type"] == "done"
            assert done["model"] == "gpt-4o"
            assert done["finish_reason"] == "stop"
            session_hash = done["session_hash"]
            assert len(session_hash) == 64

            # cas_read(session_hash) retrieves the envelope.
            envelope_bytes = nx._kernel.cas_read("/llm", "root", session_hash)
            envelope = json.loads(envelope_bytes)
            assert envelope["type"] == "llm_session_v1"
            assert envelope["model"] == "gpt-4o"
            assert envelope["request_hash"]
            assert envelope["response_hash"]
        finally:
            nx.close()

    @pytest.mark.asyncio
    async def test_streaming_error_path(self, tmp_path: Path) -> None:
        base_url, _t = _one_shot_error_server(500, '{"error":"boom"}')
        nx = await _bootstrap(tmp_path)
        try:
            nx.sys_setattr(
                "/llm",
                entry_type=DT_MOUNT,
                backend_type="openai",
                backend_name="openai_compatible",
                openai_base_url=base_url,
                openai_api_key="sk-test",
                openai_model="gpt-4o",
                openai_blob_root=str(tmp_path / "llm_spool"),
            )

            stream_path = "/llm/stream/err"
            nx._kernel.create_stream(stream_path, 16_384)
            req_bytes = json.dumps(
                {"messages": [{"role": "user", "content": "hi"}], "model": "gpt-4o"}
            ).encode("utf-8")

            with pytest.raises(Exception, match="500"):
                nx._kernel.llm_start_streaming("/llm", "root", req_bytes, stream_path)

            payload = _collect_stream(nx, stream_path)
            err_idx = payload.index("{")
            err = json.loads(payload[err_idx:])
            assert err["type"] == "error"
        finally:
            nx.close()


class TestRustAnthropicBackendMount:
    """Mount Rust AnthropicBackend via sys_setattr(backend_type="anthropic")."""

    @pytest.mark.asyncio
    async def test_streaming_round_trip(self, tmp_path: Path) -> None:
        base_url, _t = _one_shot_sse_server(_anthropic_sse_happy())
        nx = await _bootstrap(tmp_path)
        try:
            nx.sys_setattr(
                "/llm",
                entry_type=DT_MOUNT,
                backend_type="anthropic",
                backend_name="anthropic_native",
                anthropic_base_url=base_url,
                anthropic_api_key="sk-ant-test",
                anthropic_model="claude-sonnet-4-20250514",
                anthropic_blob_root=str(tmp_path / "llm_spool"),
            )

            payload, _ = _run_streaming(
                nx,
                "/llm",
                {
                    "messages": [{"role": "user", "content": "hi"}],
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1024,
                },
            )

            assert payload.startswith("Hello")
            done_idx = payload.index("{")
            done = json.loads(payload[done_idx:])
            assert done["type"] == "done"
            assert done["model"] == "claude-sonnet-4-20250514"
            assert done["finish_reason"] == "stop"
            session_hash = done["session_hash"]
            assert len(session_hash) == 64

            envelope_bytes = nx._kernel.cas_read("/llm", "root", session_hash)
            envelope = json.loads(envelope_bytes)
            assert envelope["type"] == "llm_session_v1"
            assert envelope["model"] == "claude-sonnet-4-20250514"
            assert envelope["request_hash"]
            assert envelope["response_hash"]
        finally:
            nx.close()
