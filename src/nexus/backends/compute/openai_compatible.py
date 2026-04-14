"""OpenAI-compatible LLM backend — CAS addressing + LLM transport.

Thin CASAddressingEngine subclass: registration + CONNECTION_ARGS + OpenAI client.
Follows the same composition pattern as CASLocalBackend (CAS + LocalTransport)
and CASGCSBackend (CAS + GCSTransport).

    nexus mount /zone/llm/openai --backend=openai_compatible \
        --config='{"base_url":"https://api.sudorouter.ai","api_key":"sk-..."}'
    nexus mount /zone/llm/local  --backend=openai_compatible \
        --config='{"base_url":"http://localhost:11434/v1"}'

write_content() is inherited from CASAddressingEngine — pure CAS storage.

LLM-specific methods:
    generate_streaming(request) → Iterator[dict]
        Yields CC-format content block frames from OpenAI streaming API.
    persist_session(request, response, ...) → WriteResult
        CAS persist: request + response + session envelope.

References:
    - Task #1589: LLM backend driver design
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, ClassVar

from nexus.backends.base.cas_addressing_engine import CASAddressingEngine
from nexus.backends.base.registry import ArgType, ConnectionArg, register_connector
from nexus.backends.compute.llm_transport import LLMTransport
from nexus.contracts.backend_features import BackendFeature
from nexus.contracts.exceptions import BackendError
from nexus.core.object_store import WriteResult

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)


def _build_openai_client(base_url: str, api_key: str, timeout: float) -> Any:
    """Create OpenAI client. Import deferred to avoid hard dependency."""
    try:
        from openai import OpenAI

        return OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
        )
    except ImportError:
        raise BackendError(
            "openai package not installed. Install with: pip install 'nexus-ai-fs[all]'",
            backend="openai_compatible",
        ) from None


@register_connector(
    "openai_compatible",
    description="OpenAI-compatible LLM API (OpenAI, SudoRouter, OpenRouter, Ollama)",
    category="compute",
    requires=["openai"],
)
class CASOpenAIBackend(CASAddressingEngine):
    """CAS addressing + OpenAI-compatible LLM transport.

    Thin subclass for connector registration and OpenAI client holder.
    ``write_content()`` is inherited from CASAddressingEngine — pure CAS.

    generate_streaming() yields CC-format content block frames directly.
    ManagedAgentLoop iterates the generator synchronously.

    Usage::

        backend = CASOpenAIBackend(
            base_url="https://api.sudorouter.ai", api_key="sk-...",
        )

        # Direct generator iteration (no DT_STREAM):
        for frame in backend.generate_streaming(request):
            if frame["type"] == "text":
                print(frame["text"], end="")
    """

    CONNECTION_ARGS: dict[str, ConnectionArg] = {
        "base_url": ConnectionArg(
            type=ArgType.STRING,
            description="API base URL (e.g. https://api.openai.com/v1)",
            required=True,
        ),
        "api_key": ConnectionArg(
            type=ArgType.SECRET,
            description="API key",
            required=False,
            secret=True,
            env_var="OPENAI_API_KEY",
        ),
        "default_model": ConnectionArg(
            type=ArgType.STRING,
            description="Default model (e.g. gpt-4o, claude-3-opus)",
            required=False,
            default="gpt-4o",
        ),
    }

    _BACKEND_FEATURES: ClassVar[frozenset[BackendFeature]] = frozenset(
        {
            BackendFeature.CAS,
            BackendFeature.STREAMING,
            BackendFeature.BATCH_CONTENT,
        }
    )

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        default_model: str = "gpt-4o",
        timeout: float = 120.0,
        pool: Any = None,
        pool_classifier: Any = None,
    ) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._default_model = default_model
        self._timeout = timeout

        # Optional credential pool for multi-key failover (Issue #3723).
        self._pool: Any = pool
        self._pool_classifier: Any = pool_classifier

        # Build OpenAI client (lazy import). Used when pool is None.
        self._client = _build_openai_client(base_url, api_key, timeout)

        # In-memory transport for CAS blobs
        transport = LLMTransport()

        super().__init__(transport, backend_name="openai_compatible")

        # Wire message-boundary CDC for LLM conversation dedup (Issue #1826).
        from nexus.backends.compute.message_chunking import MessageBoundaryStrategy
        from nexus.backends.engines.cdc import ChunkingStrategy

        cdc: ChunkingStrategy = MessageBoundaryStrategy(self)
        self._cdc = cdc

    @property
    def name(self) -> str:
        return "openai_compatible"

    def set_stream_manager(self, nx_or_sm: Any) -> None:
        """No-op — DT_STREAM orchestration removed. Kept for factory compatibility."""

    # ------------------------------------------------------------------
    # Streaming — CC-format content block frames
    # ------------------------------------------------------------------

    def generate_streaming(self, request: dict[str, Any]) -> Iterator[dict]:
        """Yield CC-format content block frames from OpenAI streaming API.

        Each yielded dict is a CC content block or metadata frame:
            {"type": "text", "text": "..."}
            {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
            {"type": "usage", "usage": {...}}
            {"type": "stop", "stop_reason": "..."}
            {"type": "error", "message": "..."}

        Args:
            request: Parsed request dict with ``messages`` and optional
                ``model``, ``temperature``, etc.

        Raises:
            BackendError: On missing ``messages`` or API connection failure.
        """
        if "messages" not in request:
            raise BackendError(
                "Request must contain 'messages' field",
                backend="openai_compatible",
            )

        model = request.get("model", self._default_model)
        messages = request["messages"]
        extra_params = {k: v for k, v in request.items() if k not in ("model", "messages")}

        # Pool-based credential selection with pre-first-token failover (Issue #3723).
        _pool_profile: Any = None

        if self._pool is not None:

            def _open_stream(profile: Any) -> Any:
                nonlocal _pool_profile
                _pool_profile = profile
                _cred = getattr(profile, "credential", None)
                _key = getattr(_cred, "key", None) or self._api_key
                _client = _build_openai_client(self._base_url, _key, self._timeout)
                return _client.chat.completions.create(
                    model=model,
                    messages=messages,
                    stream=True,
                    stream_options={"include_usage": True},
                    **extra_params,
                )

            try:
                stream = self._pool.execute_sync(_open_stream, self._pool_classifier)
            except Exception as e:
                raise BackendError(
                    f"LLM API call failed: {e}",
                    backend="openai_compatible",
                ) from e
        else:
            try:
                stream = self._client.chat.completions.create(
                    model=model,
                    messages=messages,
                    stream=True,
                    stream_options={"include_usage": True},
                    **extra_params,
                )
            except Exception as e:
                raise BackendError(
                    f"LLM API call failed: {e}",
                    backend="openai_compatible",
                ) from e

        usage: dict[str, int] = {}
        finish_reason: str | None = None

        # Accumulate tool_calls across streaming chunks.
        # OpenAI streams tool_calls incrementally: each chunk carries an index
        # and a partial function name or arguments fragment that must be concatenated.
        tool_calls_accum: dict[int, dict[str, Any]] = {}

        try:
            for chunk in stream:
                if chunk.choices:
                    choice = chunk.choices[0]
                    delta = choice.delta

                    # Capture finish_reason (arrives on the final choice chunk)
                    if choice.finish_reason:
                        finish_reason = choice.finish_reason

                    if delta:
                        # Yield content tokens as CC text frames
                        if delta.content:
                            yield {"type": "text", "text": delta.content}

                        # Accumulate tool_calls (incremental argument fragments)
                        if delta.tool_calls:
                            for tc_chunk in delta.tool_calls:
                                idx = tc_chunk.index
                                if idx not in tool_calls_accum:
                                    tool_calls_accum[idx] = {
                                        "id": "",
                                        "name": "",
                                        "arguments": "",
                                    }
                                entry = tool_calls_accum[idx]
                                if tc_chunk.id:
                                    entry["id"] = tc_chunk.id
                                if tc_chunk.function:
                                    if tc_chunk.function.name:
                                        entry["name"] += tc_chunk.function.name
                                    if tc_chunk.function.arguments:
                                        entry["arguments"] += tc_chunk.function.arguments

                # Capture usage from final chunk (stream_options.include_usage)
                if chunk.usage:
                    usage = {
                        "prompt_tokens": chunk.usage.prompt_tokens or 0,
                        "completion_tokens": chunk.usage.completion_tokens or 0,
                        "total_tokens": chunk.usage.total_tokens or 0,
                    }
        except Exception as e:
            if (
                _pool_profile is not None
                and self._pool is not None
                and self._pool_classifier is not None
            ):
                self._pool.mark_failure(_pool_profile, self._pool_classifier(e))
            yield {"type": "error", "message": f"LLM streaming failed: {e}"}
            return

        # Yield complete tool_use frames (after stream ends, arguments fully accumulated)
        for idx in sorted(tool_calls_accum):
            entry = tool_calls_accum[idx]
            try:
                input_data = json.loads(entry["arguments"]) if entry["arguments"] else {}
            except (json.JSONDecodeError, TypeError):
                input_data = {}
            yield {
                "type": "tool_use",
                "id": entry["id"],
                "name": entry["name"],
                "input": input_data,
            }

        # Metadata frames
        yield {"type": "usage", "usage": usage}
        yield {"type": "stop", "stop_reason": finish_reason or "stop"}

    # ------------------------------------------------------------------
    # CAS persistence — session envelope storage
    # ------------------------------------------------------------------

    def persist_session(
        self,
        request_bytes: bytes,
        response_content: str,
        model: str,
        finish_reason: str,
        usage: dict[str, int],
        latency_ms: float,
        context: "OperationContext | None" = None,
    ) -> WriteResult:
        """Persist request + response + session envelope in CAS."""
        # Store raw request in CAS
        request_result = self.write_content(request_bytes, context=context)

        # Build and store response payload
        response_payload = {
            "model": model,
            "content": response_content,
            "finish_reason": finish_reason,
            "usage": usage,
            "latency_ms": round(latency_ms, 1),
        }
        response_json = json.dumps(response_payload, separators=(",", ":")).encode("utf-8")
        response_result = self.write_content(response_json, context=context)

        # Build and store session envelope
        session = {
            "type": "llm_session_v1",
            "request_hash": request_result.content_id,
            "response_hash": response_result.content_id,
            "model": model,
            "latency_ms": round(latency_ms, 1),
        }
        session_bytes = json.dumps(session, separators=(",", ":")).encode("utf-8")
        session_result = self.write_content(session_bytes, context=context)

        logger.info(
            "LLM session persisted: model=%s tokens=%d latency=%.1fms session=%s",
            model,
            usage.get("total_tokens", 0),
            latency_ms,
            session_result.content_id[:16],
        )

        return session_result

    # === Directory operations (minimal, compute backends don't need dirs) ===

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        """No-op for compute backends."""
        pass

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        """No-op for compute backends."""
        pass
