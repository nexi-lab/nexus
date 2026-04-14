"""Tests for OpenAI-compatible LLM backend.

Tests cover:
- LLMTransport: in-memory blob operations
- CASOpenAIBackend: thin CASAddressingEngine subclass (no write_content override)
  - write_content(): inherited from CASAddressingEngine (pure CAS, no LLM call)
  - generate_streaming(): yields CC-format content block frames
  - persist_session(): CAS persist request + response + session envelope
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from nexus.backends.compute.llm_transport import LLMTransport
from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError

# =============================================================================
# LLMTransport tests
# =============================================================================


class TestLLMTransport:
    """Test in-memory blob transport."""

    def test_put_and_get(self) -> None:
        t = LLMTransport()
        t.store("key1", b"hello")
        data, version = t.fetch("key1")
        assert data == b"hello"
        assert version is None

    def test_get_missing_raises(self) -> None:
        t = LLMTransport()
        with pytest.raises(NexusFileNotFoundError):
            t.fetch("missing")

    def test_delete(self) -> None:
        t = LLMTransport()
        t.store("key1", b"data")
        t.remove("key1")
        assert not t.exists("key1")

    def test_delete_missing_raises(self) -> None:
        t = LLMTransport()
        with pytest.raises(NexusFileNotFoundError):
            t.remove("missing")

    def test_exists(self) -> None:
        t = LLMTransport()
        assert not t.exists("key1")
        t.store("key1", b"data")
        assert t.exists("key1")

    def test_get_size(self) -> None:
        t = LLMTransport()
        t.store("key1", b"hello")
        assert t.get_size("key1") == 5

    def test_get_size_missing_raises(self) -> None:
        t = LLMTransport()
        with pytest.raises(NexusFileNotFoundError):
            t.get_size("missing")

    def test_list_keys(self) -> None:
        t = LLMTransport()
        t.store("cas/ab/cd/hash1", b"data1")
        t.store("cas/ab/cd/hash2", b"data2")
        t.store("cas/ef/gh/hash3", b"data3")
        blobs, prefixes = t.list_keys("cas/ab/")
        assert len(prefixes) == 1  # cas/ab/cd/
        assert "cas/ab/cd/" in prefixes

    def test_copy_key(self) -> None:
        t = LLMTransport()
        t.store("src", b"content")
        t.copy_key("src", "dst")
        data, _ = t.fetch("dst")
        assert data == b"content"

    def test_copy_missing_raises(self) -> None:
        t = LLMTransport()
        with pytest.raises(NexusFileNotFoundError):
            t.copy_key("missing", "dst")

    def test_stream(self) -> None:
        t = LLMTransport()
        t.store("key1", b"abcdefghij")
        chunks = list(t.stream("key1", chunk_size=4))
        assert chunks == [b"abcd", b"efgh", b"ij"]

    def test_create_dir(self) -> None:
        t = LLMTransport()
        t.create_dir("dirs/mydir/")
        assert t.exists("dirs/mydir/")
        data, _ = t.fetch("dirs/mydir/")
        assert data == b""

    def test_transport_name(self) -> None:
        t = LLMTransport()
        assert t.transport_name == "llm_memory"

    def test_overwrite(self) -> None:
        t = LLMTransport()
        t.store("key1", b"old")
        t.store("key1", b"new")
        data, _ = t.fetch("key1")
        assert data == b"new"


# =============================================================================
# CASOpenAIBackend tests
# =============================================================================


def _make_backend(mock_client: MagicMock | None = None) -> tuple[Any, MagicMock]:
    """Create backend with mocked OpenAI client."""
    from nexus.backends.compute.openai_compatible import CASOpenAIBackend

    with patch("nexus.backends.compute.openai_compatible._build_openai_client") as mock_build:
        client = mock_client or MagicMock()
        mock_build.return_value = client
        backend = CASOpenAIBackend(
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
    tool_calls: list[dict[str, Any]] | None = None,
) -> list[MagicMock]:
    """Build mock OpenAI streaming chunks."""
    chunks = []
    for token in tokens:
        chunk = MagicMock()
        chunk.model = model
        chunk.usage = None
        delta = MagicMock()
        delta.content = token
        delta.tool_calls = None
        choice = MagicMock()
        choice.delta = delta
        choice.finish_reason = None
        chunk.choices = [choice]
        chunks.append(chunk)

    # Set finish_reason on the last token chunk
    if chunks:
        last_choice = chunks[-1].choices[0]
        last_choice.finish_reason = "tool_calls" if tool_calls else "stop"

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


class TestCASOpenAIBackend:
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

    def test_generate_streaming_yields_cc_frames(self) -> None:
        """generate_streaming yields CC-format dict frames."""
        client = MagicMock()
        mock_chunks = _mock_streaming_chunks(["Hello", " world", "!"])
        client.chat.completions.create.return_value = iter(mock_chunks)
        backend, _ = _make_backend(client)

        request = {"messages": [{"role": "user", "content": "Hi"}]}
        results = list(backend.generate_streaming(request))

        # Text frames
        text_frames = [f for f in results if f["type"] == "text"]
        assert len(text_frames) == 3
        assert text_frames[0] == {"type": "text", "text": "Hello"}
        assert text_frames[1] == {"type": "text", "text": " world"}
        assert text_frames[2] == {"type": "text", "text": "!"}

        # Usage frame
        usage_frames = [f for f in results if f["type"] == "usage"]
        assert len(usage_frames) == 1
        assert usage_frames[0]["usage"]["total_tokens"] == 30

        # Stop frame
        stop_frames = [f for f in results if f["type"] == "stop"]
        assert len(stop_frames) == 1
        assert stop_frames[0]["stop_reason"] == "stop"

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
        list(backend.generate_streaming(request))

        call_kwargs = client.chat.completions.create.call_args.kwargs
        assert call_kwargs["model"] == "claude-3-opus"
        assert call_kwargs["stream"] is True

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

    def test_generate_streaming_tool_calls(self) -> None:
        """Tool calls are accumulated and yielded as CC tool_use frames."""
        client = MagicMock()

        chunks: list[MagicMock] = []

        # Chunk 1: text content
        c1 = MagicMock()
        c1.model = "gpt-4o"
        c1.usage = None
        c1.choices = [MagicMock()]
        c1.choices[0].finish_reason = None
        c1.choices[0].delta = MagicMock()
        c1.choices[0].delta.content = "I'll read it."
        c1.choices[0].delta.tool_calls = None
        chunks.append(c1)

        # Chunk 2: tool_call start (id + name)
        c2 = MagicMock()
        c2.model = "gpt-4o"
        c2.usage = None
        c2.choices = [MagicMock()]
        c2.choices[0].finish_reason = None
        c2.choices[0].delta = MagicMock()
        c2.choices[0].delta.content = None
        tc_start = MagicMock()
        tc_start.index = 0
        tc_start.id = "call_abc123"
        tc_start.function = MagicMock()
        tc_start.function.name = "read_file"
        tc_start.function.arguments = ""
        c2.choices[0].delta.tool_calls = [tc_start]
        chunks.append(c2)

        # Chunk 3: tool_call argument fragment
        c3 = MagicMock()
        c3.model = "gpt-4o"
        c3.usage = None
        c3.choices = [MagicMock()]
        c3.choices[0].finish_reason = None
        c3.choices[0].delta = MagicMock()
        c3.choices[0].delta.content = None
        tc_frag1 = MagicMock()
        tc_frag1.index = 0
        tc_frag1.id = None
        tc_frag1.function = MagicMock()
        tc_frag1.function.name = None
        tc_frag1.function.arguments = '{"path":'
        c3.choices[0].delta.tool_calls = [tc_frag1]
        chunks.append(c3)

        # Chunk 4: tool_call argument fragment + finish_reason
        c4 = MagicMock()
        c4.model = "gpt-4o"
        c4.usage = None
        c4.choices = [MagicMock()]
        c4.choices[0].finish_reason = "tool_calls"
        c4.choices[0].delta = MagicMock()
        c4.choices[0].delta.content = None
        tc_frag2 = MagicMock()
        tc_frag2.index = 0
        tc_frag2.id = None
        tc_frag2.function = MagicMock()
        tc_frag2.function.name = None
        tc_frag2.function.arguments = '"main.py"}'
        c4.choices[0].delta.tool_calls = [tc_frag2]
        chunks.append(c4)

        # Chunk 5: usage
        c5 = MagicMock()
        c5.model = "gpt-4o"
        c5.choices = []
        usage_mock = MagicMock()
        usage_mock.prompt_tokens = 10
        usage_mock.completion_tokens = 20
        usage_mock.total_tokens = 30
        c5.usage = usage_mock
        chunks.append(c5)

        client.chat.completions.create.return_value = iter(chunks)
        backend, _ = _make_backend(client)

        request = {"messages": [{"role": "user", "content": "Read main.py"}]}
        results = list(backend.generate_streaming(request))

        # Text frame
        text_frames = [f for f in results if f["type"] == "text"]
        assert len(text_frames) == 1
        assert text_frames[0]["text"] == "I'll read it."

        # Tool use frame (CC format)
        tool_frames = [f for f in results if f["type"] == "tool_use"]
        assert len(tool_frames) == 1
        tc = tool_frames[0]
        assert tc["id"] == "call_abc123"
        assert tc["name"] == "read_file"
        assert tc["input"] == {"path": "main.py"}

        # Stop reason should be tool_calls
        stop_frames = [f for f in results if f["type"] == "stop"]
        assert stop_frames[0]["stop_reason"] == "tool_calls"

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
        assert backend.has_feature(BackendFeature.CAS)
        assert backend.has_feature(BackendFeature.STREAMING)
        assert not backend.has_feature(BackendFeature.ROOT_PATH)

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

    def test_set_stream_manager_is_noop(self) -> None:
        """set_stream_manager is a no-op after DT_STREAM removal."""
        backend, _ = _make_backend()
        backend.set_stream_manager(MagicMock())  # Should not raise

    def test_generate_streaming_error_yields_error_frame(self) -> None:
        """Mid-stream errors yield error frames instead of raising."""
        client = MagicMock()

        # First chunk succeeds, then iteration raises
        def _exploding_stream(**kwargs: Any) -> Any:
            def _gen():
                chunk = MagicMock()
                chunk.model = "gpt-4o"
                chunk.usage = None
                delta = MagicMock()
                delta.content = "partial"
                delta.tool_calls = None
                choice = MagicMock()
                choice.delta = delta
                choice.finish_reason = None
                chunk.choices = [choice]
                yield chunk
                raise ConnectionError("stream dropped")

            return _gen()

        client.chat.completions.create.side_effect = _exploding_stream
        backend, _ = _make_backend(client)

        request = {"messages": [{"role": "user", "content": "Hi"}]}
        results = list(backend.generate_streaming(request))

        # Should have text frame + error frame
        text_frames = [f for f in results if f["type"] == "text"]
        error_frames = [f for f in results if f["type"] == "error"]
        assert len(text_frames) == 1
        assert text_frames[0]["text"] == "partial"
        assert len(error_frames) == 1
        assert "stream dropped" in error_frames[0]["message"]
