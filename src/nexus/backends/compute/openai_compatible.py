"""OpenAI-compatible LLM backend — CAS addressing + LLM transport + streaming.

Thin CASAddressingEngine subclass: registration + CONNECTION_ARGS + OpenAI client.
Follows the same composition pattern as CASLocalBackend (CAS + LocalTransport)
and CASGCSBackend (CAS + GCSTransport).

    nexus mount /zone/llm/openai --backend=openai_compatible \
        --config='{"base_url":"https://api.sudorouter.ai","api_key":"sk-..."}'
    nexus mount /zone/llm/local  --backend=openai_compatible \
        --config='{"base_url":"http://localhost:11434/v1"}'

write_content() is inherited from CASAddressingEngine — pure CAS storage.
LLM streaming orchestration is owned by the backend via ``start_streaming()``:
StreamManager is injected at mount time by the factory/DLC layer.

LLM-specific methods:
    generate_streaming(request) → Iterator[(token, metadata)]
        Pure compute — yields tokens from OpenAI streaming API.
    persist_session(request, response, ...) → WriteResult
        CAS persist: request + response + session envelope.
    start_streaming(request_bytes, stream_path) → dict
        Orchestrates: create DT_STREAM → pump tokens → CAS persist → close.

References:
    - Task #1589: LLM backend driver design
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import queue
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, ClassVar, cast

from nexus.backends.base.cas_addressing_engine import CASAddressingEngine
from nexus.backends.base.registry import ArgType, ConnectionArg, register_connector
from nexus.backends.compute.llm_transport import LLMTransport
from nexus.contracts.backend_features import BackendFeature
from nexus.contracts.exceptions import BackendError
from nexus.core.object_store import WriteResult

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext
    from nexus.core.nexus_fs import NexusFS

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
    """CAS addressing + OpenAI-compatible LLM transport + streaming orchestration.

    Thin subclass for connector registration and OpenAI client holder.
    ``write_content()`` is inherited from CASAddressingEngine — pure CAS.
    Streaming orchestration is owned by the backend via ``start_streaming()``.

    StreamManager is injected at mount time via ``set_stream_manager()``
    by the factory/DLC layer. Without it, ``start_streaming()`` raises.

    LLM-specific methods:

    - ``generate_streaming()`` — pure compute, yields tokens
    - ``persist_session()`` — CAS persist request + response + envelope
    - ``start_streaming()`` — full orchestration: DT_STREAM → tokens → CAS

    Usage::

        backend = CASOpenAIBackend(
            base_url="https://api.sudorouter.ai", api_key="sk-...",
        )
        backend.set_stream_manager(stream_manager)  # injected at mount

        # Streaming LLM call (backend owns lifecycle):
        result = await backend.start_streaming(request_bytes, stream_path)
        # Client reads tokens via sys_read(stream_path, offset=N)
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
        # pool: CredentialPool | None — injected as Any to avoid a bricks→backends
        # import-layer violation (nexus.backends is below nexus.bricks in the tier stack).
        # pool_classifier: CredentialErrorClassifier callable (e.g. classify_openai_error),
        # also injected to keep nexus.bricks.auth imports out of this module.
        self._pool: Any = pool
        self._pool_classifier: Any = pool_classifier

        # Build OpenAI client (lazy import). Used when pool is None.
        self._client = _build_openai_client(base_url, api_key, timeout)

        # In-memory transport for CAS blobs
        transport = LLMTransport()

        super().__init__(transport, backend_name="openai_compatible")

        # NexusFS injected at mount time for DT_STREAM orchestration (Rust kernel)
        self._nx: NexusFS | None = None
        self._active_tasks: dict[str, asyncio.Task[None]] = {}

        # Wire message-boundary CDC for LLM conversation dedup (Issue #1826).
        # Must be after super().__init__ since MessageBoundaryStrategy needs
        # self as CASAddressingEngine.
        from nexus.backends.compute.message_chunking import MessageBoundaryStrategy
        from nexus.backends.engines.cdc import ChunkingStrategy

        cdc: ChunkingStrategy = MessageBoundaryStrategy(self)
        self._cdc = cdc

    @property
    def name(self) -> str:
        return "openai_compatible"

    # ------------------------------------------------------------------
    # StreamManager injection (DI at mount time)
    # ------------------------------------------------------------------

    def set_stream_manager(self, nx_or_sm: Any) -> None:
        """Inject NexusFS for DT_STREAM orchestration (Rust kernel).

        Called by factory/DLC at mount time — backend cannot create streams
        without this. Accepts NexusFS (preferred) or legacy StreamManager.
        """
        self._nx = nx_or_sm

    # ------------------------------------------------------------------
    # Streaming orchestration (owns full lifecycle)
    # ------------------------------------------------------------------

    # Default stream capacity: 8MB for LLM responses
    _DEFAULT_STREAM_CAPACITY = 8 * 1024 * 1024

    async def start_streaming(
        self,
        request_bytes: bytes,
        stream_path: str,
        *,
        capacity: int = _DEFAULT_STREAM_CAPACITY,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        """Start a streaming LLM call, delivering tokens via DT_STREAM.

        Creates a DT_STREAM at ``stream_path``, spawns a background task
        that iterates the sync generator and pushes tokens to the stream.
        Returns immediately — callers read tokens via
        ``sys_read(stream_path, offset=N)``.

        Raises:
            BackendError: If StreamManager is not injected.
        """
        if self._nx is None:
            raise BackendError(
                "LLM streaming unavailable: NexusFS not injected. "
                "Ensure backend is mounted via DLC.",
                backend="openai_compatible",
            )

        try:
            self._nx.stream_create(stream_path, capacity)
        except Exception as exc:
            raise BackendError(f"LLM streaming unavailable: {exc}") from exc

        task = asyncio.create_task(
            self._run_stream(request_bytes, stream_path),
            name=f"llm-stream:{stream_path}",
        )
        self._active_tasks[stream_path] = task

        return {"stream_path": stream_path, "status": "streaming"}

    async def cancel_stream(self, stream_path: str) -> bool:
        """Cancel an active streaming LLM call."""
        task = self._active_tasks.pop(stream_path, None)
        if task is None:
            return False

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        if self._nx is not None:
            with contextlib.suppress(Exception):
                self._nx.stream_destroy(stream_path)

        logger.info("LLM stream cancelled: %s", stream_path)
        return True

    async def _run_stream(self, request_bytes: bytes, stream_path: str) -> None:
        """Background task: pump tokens from LLM to DT_STREAM, then CAS persist."""
        assert self._nx is not None  # guaranteed by start_streaming() guard
        nx = self._nx

        _SENTINEL: object = object()
        token_q: queue.Queue[tuple[str, dict[str, Any] | None] | object | Exception] = queue.Queue(
            maxsize=4096
        )

        def _producer() -> None:
            try:
                request = json.loads(request_bytes)
                for item in self.generate_streaming(request):
                    token_q.put(item)
                token_q.put(_SENTINEL)
            except Exception as exc:
                token_q.put(exc)

        loop = asyncio.get_running_loop()
        producer_fut = loop.run_in_executor(None, _producer)

        meta: dict[str, Any] = {}
        try:
            while True:
                item = await loop.run_in_executor(None, token_q.get)
                if item is _SENTINEL:
                    break
                if isinstance(item, Exception):
                    raise item

                token_item = cast(tuple[str, dict[str, Any] | None], item)
                token: str = token_item[0]
                token_meta: dict[str, Any] | None = token_item[1]
                if token:
                    nx.stream_write_nowait(stream_path, token.encode("utf-8"))
                if token_meta is not None:
                    meta = token_meta

            # Collect all stream payloads in single Rust call (no per-frame PyO3 roundtrip)
            full_response = nx.stream_collect_all(stream_path)
            result = self.persist_session(
                request_bytes=request_bytes,
                response_content=full_response.decode("utf-8"),
                model=meta.get("model", ""),
                finish_reason=meta.get("finish_reason", "stop"),
                usage=meta.get("usage", {}),
                latency_ms=meta.get("latency_ms", 0),
            )

            done_payload: dict[str, Any] = {
                "type": "done",
                "session_hash": result.content_id,
                "model": meta.get("model", ""),
                "latency_ms": meta.get("latency_ms", 0),
                "finish_reason": meta.get("finish_reason", "stop"),
                "usage": meta.get("usage", {}),
            }
            # Include tool_calls in the done message so ManagedAgentLoop can extract them
            tool_calls = meta.get("tool_calls", [])
            if tool_calls:
                done_payload["tool_calls"] = tool_calls

            done_msg = json.dumps(done_payload, separators=(",", ":"))
            nx.stream_write_nowait(stream_path, done_msg.encode("utf-8"))
            nx.stream_close(stream_path)

            logger.info(
                "LLM stream completed: %s model=%s session=%s",
                stream_path,
                meta.get("model", ""),
                result.content_id[:16],
            )

        except asyncio.CancelledError:
            logger.info("LLM stream cancelled: %s", stream_path)
            with contextlib.suppress(Exception):
                nx.stream_close(stream_path)
            raise

        except Exception as exc:
            logger.error("LLM stream failed: %s error=%s", stream_path, exc)
            error_msg = json.dumps(
                {"type": "error", "message": str(exc)},
                separators=(",", ":"),
            )
            with contextlib.suppress(Exception):
                nx.stream_write_nowait(stream_path, error_msg.encode("utf-8"))
                nx.stream_close(stream_path)

        finally:
            with contextlib.suppress(Exception):
                await producer_fut
            self._active_tasks.pop(stream_path, None)

    @property
    def active_streams(self) -> list[str]:
        """Return VFS paths of currently active streaming LLM calls."""
        return list(self._active_tasks.keys())

    async def shutdown_streams(self) -> None:
        """Cancel all active streams. Called on kernel shutdown."""
        paths = list(self._active_tasks.keys())
        for path in paths:
            await self.cancel_stream(path)

    # ------------------------------------------------------------------
    # Streaming — pure compute, no kernel IPC
    # ------------------------------------------------------------------

    def generate_streaming(
        self, request: dict[str, Any]
    ) -> Iterator[tuple[str, dict[str, Any] | None]]:
        """Yield ``(token, None)`` per chunk, ``("", metadata)`` at end.

        Pure LLM compute — no kernel IPC, no StreamManager dependency.
        The backend's ``start_streaming()`` bridges these tokens
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

        # Pool-based credential selection with pre-first-token failover (Issue #3723).
        # execute_sync() opens the stream inside a select+retry wrapper: if the
        # initial API call fails with a retriable reason (RATE_LIMIT, OVERLOADED,
        # TIMEOUT) before any bytes are emitted, the pool marks the profile on
        # cooldown and retries with the next available credential automatically.
        # Mid-stream failures (after the first byte) cannot be retried; they call
        # mark_failure directly and re-raise. When no pool is configured, fall back
        # to the single pre-built client (no change from pre-#3723 behaviour).
        _pool_profile: Any = None

        if self._pool is not None:

            def _open_stream(profile: Any) -> Any:
                nonlocal _pool_profile
                _pool_profile = profile  # capture so mid-stream failures can mark it
                # Resolve API key from the profile's backend_key (new model) or
                # fall back to the configured api_key. The backend_key for
                # nexus-token-manager profiles encodes provider/user_email;
                # for API-key pools the key itself may be passed via account_identifier.
                _key = getattr(profile, "account_identifier", None) or self._api_key
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

        start_time = time.perf_counter()
        collected_model = model
        usage: dict[str, int] = {}
        finish_reason: str | None = None

        # Accumulate tool_calls across streaming chunks.
        # OpenAI streams tool_calls incrementally: each chunk carries an index
        # and a partial function name or arguments fragment that must be concatenated.
        tool_calls_accum: dict[int, dict[str, Any]] = {}

        try:
            for chunk in stream:
                # Capture model from response
                if chunk.model:
                    collected_model = chunk.model

                if chunk.choices:
                    choice = chunk.choices[0]
                    delta = choice.delta

                    # Capture finish_reason (arrives on the final choice chunk)
                    if choice.finish_reason:
                        finish_reason = choice.finish_reason

                    if delta:
                        # Yield content tokens
                        if delta.content:
                            yield (delta.content, None)

                        # Accumulate tool_calls (incremental argument fragments)
                        if delta.tool_calls:
                            for tc_chunk in delta.tool_calls:
                                idx = tc_chunk.index
                                if idx not in tool_calls_accum:
                                    tool_calls_accum[idx] = {
                                        "id": "",
                                        "type": "function",
                                        "function": {"name": "", "arguments": ""},
                                    }
                                entry = tool_calls_accum[idx]
                                if tc_chunk.id:
                                    entry["id"] = tc_chunk.id
                                if tc_chunk.function:
                                    if tc_chunk.function.name:
                                        entry["function"]["name"] += tc_chunk.function.name
                                    if tc_chunk.function.arguments:
                                        entry["function"]["arguments"] += (
                                            tc_chunk.function.arguments
                                        )

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
            raise BackendError(
                f"LLM streaming failed: {e}",
                backend="openai_compatible",
            ) from e

        elapsed_ms = (time.perf_counter() - start_time) * 1000

        # Build sorted tool_calls list from accumulated fragments
        tool_calls: list[dict[str, Any]] = (
            [tool_calls_accum[i] for i in sorted(tool_calls_accum)] if tool_calls_accum else []
        )

        # Final metadata message
        yield (
            "",
            {
                "model": collected_model,
                "usage": usage,
                "latency_ms": round(elapsed_ms, 1),
                "finish_reason": finish_reason or "stop",
                "tool_calls": tool_calls,
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

        Called by ``_run_stream()`` after DT_STREAM flush, or
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
