"""Tests for OpenAI-compatible LLM backend.

Tests cover:
- LLMBlobTransport: in-memory blob operations
- OpenAICompatibleBackend: thin CASAddressingEngine subclass (no write_content override)
  - write_content(): inherited from CASAddressingEngine (pure CAS, no LLM call)
  - generate_streaming(): pure LLM compute, yields tokens
  - persist_session(): CAS persist request + response + session envelope
- LLMStreamingService: DT_STREAM orchestration + CAS flush
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from nexus.backends.compute.llm_blob_transport import LLMBlobTransport
from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError

# =============================================================================
# LLMBlobTransport tests
# =============================================================================


class TestLLMBlobTransport:
    """Test in-memory blob transport."""

    def test_put_and_get(self) -> None:
        t = LLMBlobTransport()
        t.put_blob("key1", b"hello")
        data, version = t.get_blob("key1")
        assert data == b"hello"
        assert version is None

    def test_get_missing_raises(self) -> None:
        t = LLMBlobTransport()
        with pytest.raises(NexusFileNotFoundError):
            t.get_blob("missing")

    def test_delete(self) -> None:
        t = LLMBlobTransport()
        t.put_blob("key1", b"data")
        t.delete_blob("key1")
        assert not t.blob_exists("key1")

    def test_delete_missing_raises(self) -> None:
        t = LLMBlobTransport()
        with pytest.raises(NexusFileNotFoundError):
            t.delete_blob("missing")

    def test_blob_exists(self) -> None:
        t = LLMBlobTransport()
        assert not t.blob_exists("key1")
        t.put_blob("key1", b"data")
        assert t.blob_exists("key1")

    def test_get_blob_size(self) -> None:
        t = LLMBlobTransport()
        t.put_blob("key1", b"hello")
        assert t.get_blob_size("key1") == 5

    def test_get_blob_size_missing_raises(self) -> None:
        t = LLMBlobTransport()
        with pytest.raises(NexusFileNotFoundError):
            t.get_blob_size("missing")

    def test_list_blobs(self) -> None:
        t = LLMBlobTransport()
        t.put_blob("cas/ab/cd/hash1", b"data1")
        t.put_blob("cas/ab/cd/hash2", b"data2")
        t.put_blob("cas/ef/gh/hash3", b"data3")
        blobs, prefixes = t.list_blobs("cas/ab/")
        assert len(prefixes) == 1  # cas/ab/cd/
        assert "cas/ab/cd/" in prefixes

    def test_copy_blob(self) -> None:
        t = LLMBlobTransport()
        t.put_blob("src", b"content")
        t.copy_blob("src", "dst")
        data, _ = t.get_blob("dst")
        assert data == b"content"

    def test_copy_missing_raises(self) -> None:
        t = LLMBlobTransport()
        with pytest.raises(NexusFileNotFoundError):
            t.copy_blob("missing", "dst")

    def test_stream_blob(self) -> None:
        t = LLMBlobTransport()
        t.put_blob("key1", b"abcdefghij")
        chunks = list(t.stream_blob("key1", chunk_size=4))
        assert chunks == [b"abcd", b"efgh", b"ij"]

    def test_create_directory_marker(self) -> None:
        t = LLMBlobTransport()
        t.create_directory_marker("dirs/mydir/")
        assert t.blob_exists("dirs/mydir/")
        data, _ = t.get_blob("dirs/mydir/")
        assert data == b""

    def test_transport_name(self) -> None:
        t = LLMBlobTransport()
        assert t.transport_name == "llm_memory"

    def test_overwrite(self) -> None:
        t = LLMBlobTransport()
        t.put_blob("key1", b"old")
        t.put_blob("key1", b"new")
        data, _ = t.get_blob("key1")
        assert data == b"new"


# =============================================================================
# OpenAICompatibleBackend tests
# =============================================================================


def _make_backend(mock_client: MagicMock | None = None) -> tuple[Any, MagicMock]:
    """Create backend with mocked OpenAI client."""
    from nexus.backends.compute.openai_compatible import OpenAICompatibleBackend

    with patch("nexus.backends.compute.openai_compatible._build_openai_client") as mock_build:
        client = mock_client or MagicMock()
        mock_build.return_value = client
        backend = OpenAICompatibleBackend(
            base_url="https://api.test.com/v1",
            api_key="sk-test",
            default_model="gpt-4o",
        )
    return backend, client


def _mock_streaming_chunks(
    tokens: list[str],
    model: str = "gpt-4o",
    prompt_tokens: int = 10,
    completion_tokens: int = 20,
) -> list[MagicMock]:
    """Build mock OpenAI streaming chunks."""
    chunks = []
    for token in tokens:
        chunk = MagicMock()
        chunk.model = model
        chunk.usage = None
        delta = MagicMock()
        delta.content = token
        choice = MagicMock()
        choice.delta = delta
        chunk.choices = [choice]
        chunks.append(chunk)

    # Final chunk with usage (stream_options.include_usage)
    final = MagicMock()
    final.model = model
    final.choices = []
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    usage.total_tokens = prompt_tokens + completion_tokens
    final.usage = usage
    chunks.append(final)

    return chunks


class TestOpenAICompatibleBackend:
    """Test OpenAI-compatible backend with mocked API."""

    def test_name(self) -> None:
        backend, _ = _make_backend()
        assert backend.name == "openai_compatible"

    def test_write_content_is_pure_cas(self) -> None:
        """write_content() is inherited from CASAddressingEngine — no LLM call."""
        backend, client = _make_backend()

        data = b"hello world"
        result = backend.write_content(data)
        assert result.content_id
        assert result.size == len(data)

        # No LLM API call was made
        client.chat.completions.create.assert_not_called()

        # Can read back the data
        stored = backend.read_content(result.content_id)
        assert stored == data

    def test_generate_streaming_yields_tokens(self) -> None:
        """generate_streaming yields (token, None) then ("", metadata)."""
        client = MagicMock()
        mock_chunks = _mock_streaming_chunks(["Hello", " world", "!"])
        client.chat.completions.create.return_value = iter(mock_chunks)
        backend, _ = _make_backend(client)

        request = {"messages": [{"role": "user", "content": "Hi"}]}
        results = list(backend.generate_streaming(request))

        # Tokens
        tokens = [(t, m) for t, m in results if t]
        assert len(tokens) == 3
        assert tokens[0] == ("Hello", None)
        assert tokens[1] == (" world", None)
        assert tokens[2] == ("!", None)

        # Final metadata
        final = results[-1]
        assert final[0] == ""
        assert final[1] is not None
        meta = final[1]
        assert meta["model"] == "gpt-4o"
        assert meta["usage"]["total_tokens"] == 30
        assert "latency_ms" in meta

    def test_generate_streaming_custom_model(self) -> None:
        """Model from request is used."""
        client = MagicMock()
        mock_chunks = _mock_streaming_chunks(["OK"], model="claude-3-opus")
        client.chat.completions.create.return_value = iter(mock_chunks)
        backend, _ = _make_backend(client)

        request = {
            "messages": [{"role": "user", "content": "Hi"}],
            "model": "claude-3-opus",
        }
        results = list(backend.generate_streaming(request))

        call_kwargs = client.chat.completions.create.call_args.kwargs
        assert call_kwargs["model"] == "claude-3-opus"
        assert call_kwargs["stream"] is True

        meta = results[-1][1]
        assert meta["model"] == "claude-3-opus"

    def test_generate_streaming_extra_params(self) -> None:
        """Extra params (temperature, etc.) are passed through."""
        client = MagicMock()
        mock_chunks = _mock_streaming_chunks(["OK"])
        client.chat.completions.create.return_value = iter(mock_chunks)
        backend, _ = _make_backend(client)

        request = {
            "messages": [{"role": "user", "content": "Hi"}],
            "temperature": 0.7,
            "max_tokens": 100,
        }
        list(backend.generate_streaming(request))

        call_kwargs = client.chat.completions.create.call_args.kwargs
        assert call_kwargs["temperature"] == 0.7
        assert call_kwargs["max_tokens"] == 100

    def test_generate_streaming_missing_messages_raises(self) -> None:
        backend, _ = _make_backend()
        with pytest.raises(BackendError, match="must contain 'messages'"):
            list(backend.generate_streaming({"model": "gpt-4o"}))

    def test_generate_streaming_api_failure_raises(self) -> None:
        client = MagicMock()
        client.chat.completions.create.side_effect = Exception("Connection timeout")
        backend, _ = _make_backend(client)

        request = {"messages": [{"role": "user", "content": "Hi"}]}
        with pytest.raises(BackendError, match="LLM API call failed"):
            list(backend.generate_streaming(request))

    def test_persist_session(self) -> None:
        """persist_session stores request + response + envelope in CAS."""
        backend, _ = _make_backend()

        request_bytes = json.dumps({"messages": [{"role": "user", "content": "Hello"}]}).encode()

        result = backend.persist_session(
            request_bytes=request_bytes,
            response_content="Hi there!",
            model="gpt-4o",
            finish_reason="stop",
            usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            latency_ms=42.5,
        )

        # Read session envelope
        session = json.loads(backend.read_content(result.content_id))
        assert session["type"] == "llm_session_v1"
        assert session["model"] == "gpt-4o"
        assert "request_hash" in session
        assert "response_hash" in session

        # Read request
        req_data = json.loads(backend.read_content(session["request_hash"]))
        assert req_data["messages"][0]["content"] == "Hello"

        # Read response
        resp_data = json.loads(backend.read_content(session["response_hash"]))
        assert resp_data["content"] == "Hi there!"
        assert resp_data["usage"]["total_tokens"] == 8
        assert resp_data["finish_reason"] == "stop"

    def test_capabilities(self) -> None:
        from nexus.contracts.backend_features import BackendFeature

        backend, _ = _make_backend()
        assert backend.has_capability(BackendFeature.CAS)
        assert backend.has_capability(BackendFeature.STREAMING)
        assert not backend.has_capability(BackendFeature.ROOT_PATH)

    def test_mkdir_rmdir_noop(self) -> None:
        """Directory ops are no-ops for compute backends."""
        backend, _ = _make_backend()
        backend.mkdir("/some/path")  # Should not raise
        backend.rmdir("/some/path")  # Should not raise

    def test_content_exists(self) -> None:
        backend, _ = _make_backend()
        result = backend.write_content(b"test data")
        assert backend.content_exists(result.content_id)

    def test_delete_content(self) -> None:
        backend, _ = _make_backend()
        result = backend.write_content(b"test data")
        backend.delete_content(result.content_id)
        with pytest.raises(NexusFileNotFoundError):
            backend.read_content(result.content_id)

    def test_content_exists_via_cas(self) -> None:
        """Verify content_exists works via CASAddressingEngine."""
        backend, _ = _make_backend()
        result = backend.write_content(b"cas test data")
        assert backend.content_exists(result.content_id)


# =============================================================================
# LLMStreamingService tests
# =============================================================================


class TestLLMStreamingService:
    """Test DT_STREAM orchestration with mock backend + stream manager."""

    @pytest.fixture()
    def mock_stream_manager(self) -> MagicMock:
        """Create a mock StreamManager that stores writes in a list."""
        sm = MagicMock()
        # Track writes for verification
        sm._written: list[bytes] = []
        sm._closed = False

        def _write_nowait(path: str, data: bytes) -> int:
            sm._written.append(data)
            return len(data)

        def _collect_all(path: str) -> bytes:
            return b"".join(sm._written)

        def _signal_close(path: str) -> None:
            sm._closed = True

        sm.stream_write_nowait.side_effect = _write_nowait
        sm.collect_all.side_effect = _collect_all
        sm.signal_close.side_effect = _signal_close
        return sm

    @pytest.fixture()
    def mock_backend(self) -> MagicMock:
        """Create a mock OpenAICompatibleBackend."""
        backend = MagicMock()

        def _generate_streaming(request: dict) -> list[tuple[str, dict | None]]:
            return [
                ("Hello", None),
                (" world", None),
                ("!", None),
                (
                    "",
                    {
                        "model": "gpt-4o",
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 3,
                            "total_tokens": 13,
                        },
                        "latency_ms": 50.0,
                    },
                ),
            ]

        backend.generate_streaming.side_effect = _generate_streaming

        persist_result = MagicMock()
        persist_result.content_id = "abc123deadbeef"
        backend.persist_session.return_value = persist_result

        return backend

    @pytest.mark.asyncio()
    async def test_start_stream(
        self, mock_stream_manager: MagicMock, mock_backend: MagicMock
    ) -> None:
        """start_stream creates DT_STREAM and returns immediately."""
        from nexus.services.llm_streaming_service import (
            LLMStreamingService,
        )

        service = LLMStreamingService(stream_manager=mock_stream_manager, backend=mock_backend)

        request = json.dumps({"messages": [{"role": "user", "content": "Hi"}]}).encode()
        result = await service.start_stream(request, "/zone/llm/.streams/s1")

        assert result["status"] == "streaming"
        assert result["stream_path"] == "/zone/llm/.streams/s1"
        mock_stream_manager.create.assert_called_once()

        # Wait for background task to complete
        await asyncio.sleep(0.1)

    @pytest.mark.asyncio()
    async def test_stream_delivers_tokens(
        self, mock_stream_manager: MagicMock, mock_backend: MagicMock
    ) -> None:
        """Tokens are pushed to DT_STREAM, then CAS persist + signal_close."""
        from nexus.services.llm_streaming_service import (
            LLMStreamingService,
        )

        service = LLMStreamingService(stream_manager=mock_stream_manager, backend=mock_backend)

        request = json.dumps({"messages": [{"role": "user", "content": "Hi"}]}).encode()
        await service.start_stream(request, "/zone/llm/.streams/s2")

        # Wait for background task
        await asyncio.sleep(0.15)

        # Verify tokens were written to stream
        written = mock_stream_manager._written
        # Tokens: "Hello", " world", "!" + done message
        assert len(written) >= 3
        assert written[0] == b"Hello"
        assert written[1] == b" world"
        assert written[2] == b"!"

        # Verify done message
        done_msg = json.loads(written[-1])
        assert done_msg["type"] == "done"
        assert done_msg["session_hash"] == "abc123deadbeef"

        # Verify CAS persist was called
        mock_backend.persist_session.assert_called_once()
        call_kwargs = mock_backend.persist_session.call_args
        assert call_kwargs.kwargs["response_content"] == "Hello world!"
        assert call_kwargs.kwargs["model"] == "gpt-4o"

        # Verify stream was closed
        assert mock_stream_manager._closed

    @pytest.mark.asyncio()
    async def test_stream_error_handling(self, mock_stream_manager: MagicMock) -> None:
        """On LLM failure, error is written to stream and stream is closed."""
        from nexus.services.llm_streaming_service import (
            LLMStreamingService,
        )

        backend = MagicMock()
        backend.generate_streaming.side_effect = BackendError(
            "API down", backend="openai_compatible"
        )

        service = LLMStreamingService(stream_manager=mock_stream_manager, backend=backend)

        request = json.dumps({"messages": [{"role": "user", "content": "Hi"}]}).encode()
        await service.start_stream(request, "/zone/llm/.streams/err")

        # Wait for background task
        await asyncio.sleep(0.15)

        # Verify error message was written
        written = mock_stream_manager._written
        assert len(written) >= 1
        error_msg = json.loads(written[-1])
        assert error_msg["type"] == "error"

        # Stream was closed
        assert mock_stream_manager._closed

    @pytest.mark.asyncio()
    async def test_cancel_stream(self, mock_stream_manager: MagicMock) -> None:
        """cancel_stream cancels the background task and destroys the stream."""
        from nexus.services.llm_streaming_service import (
            LLMStreamingService,
        )

        # Slow backend that blocks
        backend = MagicMock()

        def _slow_generate(request: dict) -> list[tuple[str, dict | None]]:
            import time

            time.sleep(10)
            return [("x", None), ("", {"model": "m", "usage": {}, "latency_ms": 0})]

        backend.generate_streaming.side_effect = _slow_generate

        service = LLMStreamingService(stream_manager=mock_stream_manager, backend=backend)

        request = json.dumps({"messages": [{"role": "user", "content": "Hi"}]}).encode()
        await service.start_stream(request, "/zone/llm/.streams/cancel")

        assert "/zone/llm/.streams/cancel" in service.active_streams
        cancelled = await service.cancel_stream("/zone/llm/.streams/cancel")
        assert cancelled
        assert "/zone/llm/.streams/cancel" not in service.active_streams

    @pytest.mark.asyncio()
    async def test_active_streams(
        self, mock_stream_manager: MagicMock, mock_backend: MagicMock
    ) -> None:
        """active_streams tracks running streams."""
        from nexus.services.llm_streaming_service import (
            LLMStreamingService,
        )

        service = LLMStreamingService(stream_manager=mock_stream_manager, backend=mock_backend)

        assert service.active_streams == []

        request = json.dumps({"messages": [{"role": "user", "content": "Hi"}]}).encode()
        await service.start_stream(request, "/zone/llm/.streams/track")

        # Task is active briefly
        assert "/zone/llm/.streams/track" in service.active_streams

        # Wait for completion
        await asyncio.sleep(0.15)
        assert "/zone/llm/.streams/track" not in service.active_streams
