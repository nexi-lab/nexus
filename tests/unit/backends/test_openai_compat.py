"""Tests for OpenAI-compatible LLM backend (sync MVP).

Tests cover:
- LLMBlobTransport: in-memory blob operations
- OpenAICompatibleBackend: CAS write → LLM call → session envelope
- Error handling: invalid JSON, missing messages, API failure
"""

from __future__ import annotations

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


def _mock_completion(
    content: str = "Hello! I'm an AI assistant.",
    model: str = "gpt-4o",
    prompt_tokens: int = 10,
    completion_tokens: int = 20,
    finish_reason: str = "stop",
) -> MagicMock:
    """Build a mock OpenAI ChatCompletion response."""
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    usage.total_tokens = prompt_tokens + completion_tokens

    message = MagicMock()
    message.content = content

    choice = MagicMock()
    choice.message = message
    choice.finish_reason = finish_reason

    completion = MagicMock()
    completion.choices = [choice]
    completion.model = model
    completion.usage = usage
    return completion


class TestOpenAICompatibleBackend:
    """Test OpenAI-compatible backend with mocked API."""

    def _make_backend(self, mock_client: MagicMock | None = None) -> Any:
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

    def test_name(self) -> None:
        backend, _ = self._make_backend()
        assert backend.name == "openai_compatible"

    def test_write_and_read_session(self) -> None:
        """Write request → LLM call → read session envelope."""
        client = MagicMock()
        client.chat.completions.create.return_value = _mock_completion()
        backend, _ = self._make_backend(client)

        request = {"messages": [{"role": "user", "content": "Hello"}]}
        request_bytes = json.dumps(request).encode()

        result = backend.write_content(request_bytes)
        assert result.content_hash
        assert result.size > 0

        # Read session envelope
        session_bytes = backend.read_content(result.content_hash)
        session = json.loads(session_bytes)
        assert session["type"] == "llm_session_v1"
        assert "request_hash" in session
        assert "response_hash" in session
        assert session["model"] == "gpt-4o"

    def test_read_request_and_response(self) -> None:
        """Verify request and response are readable from CAS."""
        client = MagicMock()
        client.chat.completions.create.return_value = _mock_completion(content="The answer is 42.")
        backend, _ = self._make_backend(client)

        request = {"messages": [{"role": "user", "content": "What is the answer?"}]}
        result = backend.write_content(json.dumps(request).encode())

        session = json.loads(backend.read_content(result.content_hash))

        # Read request
        req_data = json.loads(backend.read_content(session["request_hash"]))
        assert req_data["messages"][0]["content"] == "What is the answer?"

        # Read response
        resp_data = json.loads(backend.read_content(session["response_hash"]))
        assert resp_data["content"] == "The answer is 42."
        assert resp_data["usage"]["total_tokens"] == 30

    def test_custom_model(self) -> None:
        """Model from request overrides default."""
        client = MagicMock()
        client.chat.completions.create.return_value = _mock_completion(model="claude-3-opus")
        backend, _ = self._make_backend(client)

        request = {
            "messages": [{"role": "user", "content": "Hi"}],
            "model": "claude-3-opus",
        }
        result = backend.write_content(json.dumps(request).encode())

        # Verify the API was called with the custom model
        call_kwargs = client.chat.completions.create.call_args
        assert call_kwargs.kwargs["model"] == "claude-3-opus"

        session = json.loads(backend.read_content(result.content_hash))
        assert session["model"] == "claude-3-opus"

    def test_extra_params_passed_through(self) -> None:
        """Extra parameters (temperature, etc.) are passed to the API."""
        client = MagicMock()
        client.chat.completions.create.return_value = _mock_completion()
        backend, _ = self._make_backend(client)

        request = {
            "messages": [{"role": "user", "content": "Hi"}],
            "temperature": 0.7,
            "max_tokens": 100,
        }
        backend.write_content(json.dumps(request).encode())

        call_kwargs = client.chat.completions.create.call_args.kwargs
        assert call_kwargs["temperature"] == 0.7
        assert call_kwargs["max_tokens"] == 100

    def test_invalid_json_raises(self) -> None:
        backend, _ = self._make_backend()
        with pytest.raises(BackendError, match="Invalid request JSON"):
            backend.write_content(b"not json {{{")

    def test_missing_messages_raises(self) -> None:
        backend, _ = self._make_backend()
        with pytest.raises(BackendError, match="must contain 'messages'"):
            backend.write_content(json.dumps({"model": "gpt-4o"}).encode())

    def test_api_failure_raises(self) -> None:
        client = MagicMock()
        client.chat.completions.create.side_effect = Exception("Connection timeout")
        backend, _ = self._make_backend(client)

        request = {"messages": [{"role": "user", "content": "Hi"}]}
        with pytest.raises(BackendError, match="LLM API call failed"):
            backend.write_content(json.dumps(request).encode())

    def test_capabilities(self) -> None:
        from nexus.contracts.capabilities import ConnectorCapability

        backend, _ = self._make_backend()
        assert backend.has_capability(ConnectorCapability.CAS)
        assert backend.has_capability(ConnectorCapability.STREAMING)
        assert not backend.has_capability(ConnectorCapability.ROOT_PATH)

    def test_mkdir_rmdir_noop(self) -> None:
        """Directory ops are no-ops for compute backends."""
        backend, _ = self._make_backend()
        backend.mkdir("/some/path")  # Should not raise
        backend.rmdir("/some/path")  # Should not raise

    def test_delete_content(self) -> None:
        """Verify content can be deleted."""
        client = MagicMock()
        client.chat.completions.create.return_value = _mock_completion()
        backend, _ = self._make_backend(client)

        request = {"messages": [{"role": "user", "content": "Hi"}]}
        result = backend.write_content(json.dumps(request).encode())

        # Should not raise
        backend.delete_content(result.content_hash)

        # Now reading should fail
        with pytest.raises(NexusFileNotFoundError):
            backend.read_content(result.content_hash)

    def test_content_exists(self) -> None:
        """Verify content_exists works via CASAddressingEngine."""
        client = MagicMock()
        client.chat.completions.create.return_value = _mock_completion()
        backend, _ = self._make_backend(client)

        request = {"messages": [{"role": "user", "content": "Hi"}]}
        result = backend.write_content(json.dumps(request).encode())
        assert backend.content_exists(result.content_hash)

    def test_get_content_size(self) -> None:
        """Verify get_content_size returns correct size."""
        client = MagicMock()
        client.chat.completions.create.return_value = _mock_completion()
        backend, _ = self._make_backend(client)

        request = {"messages": [{"role": "user", "content": "Hi"}]}
        result = backend.write_content(json.dumps(request).encode())

        size = backend.get_content_size(result.content_hash)
        # Session envelope is a small JSON
        assert size > 0
