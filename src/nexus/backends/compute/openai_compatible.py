"""OpenAI-compatible LLM backend — CAS addressing + LLM transport.

Thin CASAddressingEngine subclass: registration + CONNECTION_ARGS + OpenAI client.
Follows the same composition pattern as CASLocalBackend (CAS + LocalTransport)
and CASGCSBackend (CAS + GCSTransport).

    nexus mount /zone/llm/openai --backend=openai_compatible \
        --config='{"base_url":"https://api.sudorouter.ai","api_key":"sk-..."}'
    nexus mount /zone/llm/local  --backend=openai_compatible \
        --config='{"base_url":"http://localhost:11434/v1"}'

write_content() is inherited from CASAddressingEngine — pure CAS storage,
no LLM logic. LLM call orchestration lives in the service layer
(LLMStreamingService), not in the storage driver.

LLM-specific methods (additions, not overrides):
    generate_streaming(request) → Iterator[(token, metadata)]
        Pure compute — yields tokens from OpenAI streaming API.
    persist_session(request, response, ...) → WriteResult
        CAS persist: request + response + session envelope.

References:
    - Task #1589: LLM backend driver design
"""

from __future__ import annotations

import json
import logging
import time
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
class OpenAICompatibleBackend(CASAddressingEngine):
    """CAS addressing + OpenAI-compatible LLM transport.

    Thin subclass for connector registration and OpenAI client holder.
    ``write_content()`` is inherited from CASAddressingEngine — pure CAS, no override.
    LLM orchestration lives in ``LLMStreamingService``.

    LLM-specific methods (additions, not overrides):

    - ``generate_streaming()`` — pure compute, yields tokens
    - ``persist_session()`` — CAS persist request + response + envelope

    Usage::

        backend = OpenAICompatibleBackend(
            base_url="https://api.sudorouter.ai", api_key="sk-...",
        )
        # Standard CAS write (inherited, no LLM call):
        backend.write_content(b"raw data")

        # LLM streaming (service orchestrates):
        for token, meta in backend.generate_streaming(request_dict):
            stream.push(token)
        backend.persist_session(req_bytes, full_resp, **meta)
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
    ) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._default_model = default_model

        # Build OpenAI client (lazy import)
        self._client = _build_openai_client(base_url, api_key, timeout)

        # In-memory transport for CAS blobs
        transport = LLMTransport()

        super().__init__(transport, backend_name="openai_compatible")

    @property
    def name(self) -> str:
        return "openai_compatible"

    # ------------------------------------------------------------------
    # Streaming — pure compute, no kernel IPC
    # ------------------------------------------------------------------

    def generate_streaming(
        self, request: dict[str, Any]
    ) -> Iterator[tuple[str, dict[str, Any] | None]]:
        """Yield ``(token, None)`` per chunk, ``("", metadata)`` at end.

        Pure LLM compute — no kernel IPC, no StreamManager dependency.
        The service layer (LLMStreamingService) bridges these tokens
        to DT_STREAM for real-time fan-out.

        Args:
            request: Parsed request dict with ``messages`` and optional
                ``model``, ``temperature``, etc.

        Yields:
            ``(token_str, None)`` for each content token.
            ``("", metadata_dict)`` as the final item with model/usage/latency.

        Raises:
            BackendError: On API failure or missing ``messages``.
        """
        if "messages" not in request:
            raise BackendError(
                "Request must contain 'messages' field",
                backend="openai_compatible",
            )

        model = request.get("model", self._default_model)
        messages = request["messages"]
        extra_params = {k: v for k, v in request.items() if k not in ("model", "messages")}

        start_time = time.perf_counter()
        collected_model = model
        usage: dict[str, int] = {}

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

        try:
            for chunk in stream:
                # Capture model from response
                if chunk.model:
                    collected_model = chunk.model

                # Yield content tokens
                if chunk.choices:
                    delta = chunk.choices[0].delta
                    token = (delta.content or "") if delta else ""
                    if token:
                        yield (token, None)

                # Capture usage from final chunk (stream_options.include_usage)
                if chunk.usage:
                    usage = {
                        "prompt_tokens": chunk.usage.prompt_tokens or 0,
                        "completion_tokens": chunk.usage.completion_tokens or 0,
                        "total_tokens": chunk.usage.total_tokens or 0,
                    }
        except Exception as e:
            raise BackendError(
                f"LLM streaming failed: {e}",
                backend="openai_compatible",
            ) from e

        elapsed_ms = (time.perf_counter() - start_time) * 1000

        # Final metadata message
        yield (
            "",
            {
                "model": collected_model,
                "usage": usage,
                "latency_ms": round(elapsed_ms, 1),
            },
        )

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
        """Persist request + response + session envelope in CAS.

        Called by LLMStreamingService after DT_STREAM flush, or
        directly by callers for sync (non-streaming) LLM calls.
        Uses inherited ``write_content()`` (pure CAS) internally.

        Args:
            request_bytes: Raw request JSON bytes.
            response_content: Full LLM response text.
            model: Model name from the API response.
            finish_reason: Finish reason (e.g. "stop", "length").
            usage: Token usage dict (prompt_tokens, completion_tokens, total_tokens).
            latency_ms: End-to-end latency in milliseconds.
            context: Operation context (optional).

        Returns:
            WriteResult with content_hash of the session envelope.
        """
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
