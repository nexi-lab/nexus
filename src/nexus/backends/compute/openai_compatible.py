"""OpenAI-compatible LLM backend — CAS addressing over any OpenAI API.

Composes CASBackend (addressing) + LLMBlobTransport (in-memory I/O)
to expose LLM inference as a VFS-mountable backend.

    nexus mount /zone/llm/openai --backend=openai_compatible \
        --config='{"base_url":"https://api.sudorouter.ai","api_key":"sk-..."}'
    nexus mount /zone/llm/local  --backend=openai_compatible \
        --config='{"base_url":"http://localhost:11434/v1"}'

Write path (sync MVP):
    1. Agent writes request JSON via sys_write
    2. Backend calls OpenAI chat.completions.create (sync)
    3. Response stored in CAS
    4. Returns WriteResult(hash(request+response), size)

Read path: Standard CAS — read_content(hash) returns stored data.

Streaming (Step 2, DT_STREAM) will add async token-by-token delivery.

References:
    - Task #1589: LLM backend driver design
    - Plan: curious-gliding-orbit.md
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, ClassVar

from nexus.backends.base.cas_backend import CASBackend
from nexus.backends.base.registry import ArgType, ConnectionArg, register_connector
from nexus.backends.compute.llm_blob_transport import LLMBlobTransport
from nexus.contracts.capabilities import ConnectorCapability
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
class OpenAICompatibleBackend(CASBackend):
    """CAS addressing + OpenAI-compatible LLM transport.

    Sync MVP: write_content stores request + calls LLM + stores response.
    All data lives in CAS (in-memory transport, flushed to durable store later).

    Usage::

        backend = OpenAICompatibleBackend(
            base_url="https://api.sudorouter.ai",
            api_key="sk-...",
            default_model="gpt-4o",
        )
        # Write request → triggers LLM call
        result = backend.write_content(json.dumps({
            "messages": [{"role": "user", "content": "Hello"}]
        }).encode())
        # Read response
        data = backend.read_content(result.content_hash)
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

    _CAPABILITIES: ClassVar[frozenset[ConnectorCapability]] = frozenset(
        {
            ConnectorCapability.CAS,
            ConnectorCapability.STREAMING,
            ConnectorCapability.BATCH_CONTENT,
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
        transport = LLMBlobTransport()

        super().__init__(transport, backend_name="openai_compatible")

    @property
    def name(self) -> str:
        return "openai_compatible"

    # === LLM-specific write ===

    def write_content(
        self, content: bytes, context: "OperationContext | None" = None
    ) -> WriteResult:
        """Write request JSON, call LLM, store both request and response in CAS.

        The request is parsed as JSON with at minimum a ``messages`` field.
        The LLM response is stored as a separate CAS entry. A "session"
        envelope containing both request hash and response hash is the
        returned WriteResult.

        Args:
            content: JSON-encoded request (``{"messages": [...], "model": "...", ...}``)
            context: Operation context (optional)

        Returns:
            WriteResult with content_hash of the session envelope.
        """
        # Store the raw request in CAS first
        request_result = super().write_content(content, context=context)

        # Parse request
        try:
            request = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise BackendError(
                f"Invalid request JSON: {e}",
                backend="openai_compatible",
            ) from e

        if "messages" not in request:
            raise BackendError(
                "Request must contain 'messages' field",
                backend="openai_compatible",
            )

        # Call LLM
        model = request.pop("model", self._default_model)
        messages = request.pop("messages")
        extra_params = request  # remaining keys passed through

        start_time = time.perf_counter()
        try:
            completion = self._client.chat.completions.create(
                model=model,
                messages=messages,
                stream=False,
                **extra_params,
            )
        except Exception as e:
            raise BackendError(
                f"LLM API call failed: {e}",
                backend="openai_compatible",
            ) from e

        elapsed_ms = (time.perf_counter() - start_time) * 1000

        # Extract response
        choice = completion.choices[0] if completion.choices else None
        response_content = choice.message.content if choice and choice.message else ""
        finish_reason = choice.finish_reason if choice else "unknown"

        # Build response payload
        response_payload = {
            "model": completion.model,
            "content": response_content,
            "finish_reason": finish_reason,
            "usage": {
                "prompt_tokens": completion.usage.prompt_tokens if completion.usage else 0,
                "completion_tokens": completion.usage.completion_tokens if completion.usage else 0,
                "total_tokens": completion.usage.total_tokens if completion.usage else 0,
            },
            "latency_ms": round(elapsed_ms, 1),
        }
        response_bytes = json.dumps(response_payload, separators=(",", ":")).encode("utf-8")

        # Store response in CAS
        response_result = super().write_content(response_bytes, context=context)

        # Build session envelope (links request + response)
        session = {
            "type": "llm_session_v1",
            "request_hash": request_result.content_hash,
            "response_hash": response_result.content_hash,
            "model": completion.model,
            "latency_ms": round(elapsed_ms, 1),
        }
        session_bytes = json.dumps(session, separators=(",", ":")).encode("utf-8")

        # Store session envelope in CAS
        session_result = super().write_content(session_bytes, context=context)

        logger.info(
            "LLM call completed: model=%s tokens=%d latency=%.1fms session=%s",
            completion.model,
            completion.usage.total_tokens if completion.usage else 0,
            elapsed_ms,
            session_result.content_hash[:16],
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
