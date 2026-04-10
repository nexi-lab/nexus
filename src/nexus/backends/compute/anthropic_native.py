"""Anthropic-native LLM backend — CAS addressing + Claude API + streaming.

Native Anthropic SDK backend that avoids the OpenAI translation layer.
Benefits over CASOpenAIBackend + SudoRouter translation:
- Tool calls arrive as complete JSON (no incremental argument concatenation)
- Native extended_thinking support
- Native prompt caching (cache_control)
- Native content block streaming (content_block_start/delta/stop)
- No translation overhead

    nexus mount /zone/llm/anthropic --backend=anthropic_native \
        --config='{"api_key":"sk-ant-...", "default_model":"claude-sonnet-4-20250514"}'

Uses the same CAS persistence pattern as CASOpenAIBackend:
- write_content() inherited from CASAddressingEngine (pure CAS)
- persist_session() stores request + response + envelope
- start_streaming() orchestrates DT_STREAM lifecycle

References:
    - Task #1589: LLM backend driver design
    - docs/architecture/nexus-agent-plan.md §1.2
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


def _build_anthropic_client(api_key: str, base_url: str | None, timeout: float) -> Any:
    """Create Anthropic client. Import deferred to avoid hard dependency."""
    try:
        from anthropic import Anthropic

        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout}
        if base_url:
            kwargs["base_url"] = base_url
        return Anthropic(**kwargs)
    except ImportError:
        raise BackendError(
            "anthropic package not installed. Install with: pip install anthropic",
            backend="anthropic_native",
        ) from None


@register_connector(
    "anthropic_native",
    description="Native Anthropic Claude API (direct SDK, no translation)",
    category="compute",
    requires=["anthropic"],
)
class CASAnthropicBackend(CASAddressingEngine):
    """CAS addressing + native Anthropic Claude SDK + streaming orchestration.

    Uses the Anthropic Python SDK directly for:
    - Native tool_use content blocks (complete JSON, no incremental concatenation)
    - Native content_block_start/delta/stop streaming events
    - Native extended_thinking support (future)

    StreamManager is injected at mount time via ``set_stream_manager()``
    by the factory/DLC layer.
    """

    CONNECTION_ARGS: dict[str, ConnectionArg] = {
        "api_key": ConnectionArg(
            type=ArgType.SECRET,
            description="Anthropic API key",
            required=True,
            secret=True,
            env_var="ANTHROPIC_API_KEY",
        ),
        "base_url": ConnectionArg(
            type=ArgType.STRING,
            description="API base URL (optional, for proxies like SudoRouter /v1/messages)",
            required=False,
        ),
        "default_model": ConnectionArg(
            type=ArgType.STRING,
            description="Default model (e.g. claude-sonnet-4-20250514)",
            required=False,
            default="claude-sonnet-4-20250514",
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
        api_key: str,
        base_url: str | None = None,
        default_model: str = "claude-sonnet-4-20250514",
        timeout: float = 120.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._default_model = default_model

        self._client = _build_anthropic_client(api_key, base_url, timeout)

        transport = LLMTransport()
        super().__init__(transport, backend_name="anthropic_native")

        self._nx: NexusFS | None = None
        self._active_tasks: dict[str, asyncio.Task[None]] = {}

        from nexus.backends.compute.message_chunking import MessageBoundaryStrategy
        from nexus.backends.engines.cdc import ChunkingStrategy

        cdc: ChunkingStrategy = MessageBoundaryStrategy(self)
        self._cdc = cdc

    @property
    def name(self) -> str:
        return "anthropic_native"

    def set_stream_manager(self, nx_or_sm: Any) -> None:
        """Inject NexusFS for DT_STREAM orchestration (Rust kernel).

        Called by factory/DLC at mount time. Accepts NexusFS (preferred).
        """
        self._nx = nx_or_sm

    # ------------------------------------------------------------------
    # Streaming orchestration (owns full lifecycle)
    # ------------------------------------------------------------------

    _DEFAULT_STREAM_CAPACITY = 8 * 1024 * 1024

    async def start_streaming(
        self,
        request_bytes: bytes,
        stream_path: str,
        *,
        capacity: int = _DEFAULT_STREAM_CAPACITY,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        """Start a streaming LLM call via Anthropic API."""
        if self._nx is None:
            raise BackendError(
                "LLM streaming unavailable: NexusFS not injected. "
                "Ensure backend is mounted via DLC.",
                backend="anthropic_native",
            )

        try:
            self._nx.stream_create(stream_path, capacity)
        except Exception as exc:
            raise BackendError(f"LLM streaming unavailable: {exc}") from exc

        task = asyncio.create_task(
            self._run_stream(request_bytes, stream_path),
            name=f"anthropic-stream-{stream_path}",
        )
        self._active_tasks[stream_path] = task
        return {"status": "streaming", "stream_path": stream_path}

    async def cancel_stream(self, stream_path: str) -> bool:
        """Cancel an active streaming task."""
        task = self._active_tasks.pop(stream_path, None)
        if task is None:
            return False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if self._nx is not None:
            with contextlib.suppress(Exception):
                self._nx.stream_destroy(stream_path)
        return True

    async def _run_stream(self, request_bytes: bytes, stream_path: str) -> None:
        """Background task: pump tokens from Anthropic API to DT_STREAM, then CAS persist."""
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
                finish_reason=meta.get("finish_reason", "end_turn"),
                usage=meta.get("usage", {}),
                latency_ms=meta.get("latency_ms", 0),
            )

            done_payload: dict[str, Any] = {
                "type": "done",
                "session_hash": result.content_id,
                "model": meta.get("model", ""),
                "latency_ms": meta.get("latency_ms", 0),
                "finish_reason": meta.get("finish_reason", "end_turn"),
                "usage": meta.get("usage", {}),
            }
            tool_calls = meta.get("tool_calls", [])
            if tool_calls:
                done_payload["tool_calls"] = tool_calls

            done_msg = json.dumps(done_payload, separators=(",", ":"))
            nx.stream_write_nowait(stream_path, done_msg.encode("utf-8"))
            nx.stream_close(stream_path)

            logger.info(
                "Anthropic stream completed: %s model=%s session=%s",
                stream_path,
                meta.get("model", ""),
                result.content_id[:16],
            )

        except asyncio.CancelledError:
            logger.info("Anthropic stream cancelled: %s", stream_path)
            with contextlib.suppress(Exception):
                nx.stream_close(stream_path)
            raise

        except Exception as exc:
            logger.error("Anthropic stream failed: %s error=%s", stream_path, exc)
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

    # ------------------------------------------------------------------
    # Streaming — pure compute (Anthropic native)
    # ------------------------------------------------------------------

    def generate_streaming(
        self, request: dict[str, Any]
    ) -> Iterator[tuple[str, dict[str, Any] | None]]:
        """Yield ``(token, None)`` per chunk, ``("", metadata)`` at end.

        Uses Anthropic Messages API with native streaming.
        Tool calls arrive as complete content blocks (no incremental
        argument concatenation needed — unlike OpenAI streaming).

        Args:
            request: Dict with ``messages`` and optional ``model``, ``system``,
                ``max_tokens``, ``tools``, ``temperature``, etc.

        Yields:
            ``(token_str, None)`` for each text delta.
            ``("", metadata_dict)`` as the final item with model/usage/tool_calls.
        """
        if "messages" not in request:
            raise BackendError(
                "Request must contain 'messages' field",
                backend="anthropic_native",
            )

        model = request.get("model", self._default_model)
        messages = request["messages"]
        max_tokens = request.get("max_tokens", 8192)

        # Build Anthropic-specific kwargs
        # Note: do NOT pass stream=True — client.messages.stream() handles that.
        api_kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._convert_messages(messages),
            "max_tokens": max_tokens,
        }

        # System prompt (Anthropic uses top-level 'system', not a message)
        system = request.get("system")
        if not system:
            # Extract system from messages if present
            for msg in messages:
                if msg.get("role") == "system":
                    system = msg.get("content", "")
                    break
        if system:
            api_kwargs["system"] = system

        # Tools (convert from OpenAI format to Anthropic format)
        tools = request.get("tools")
        if tools:
            api_kwargs["tools"] = self._convert_tools(tools)

        # Pass through optional params
        for key in ("temperature", "top_p", "top_k", "stop_sequences"):
            if key in request:
                api_kwargs[key] = request[key]

        start_time = time.perf_counter()
        collected_model = model
        usage: dict[str, int] = {}
        finish_reason: str | None = None
        tool_calls: list[dict[str, Any]] = []

        # Track current tool_use block being built
        current_tool: dict[str, Any] | None = None

        try:
            with self._client.messages.stream(**api_kwargs) as stream:
                for event in stream:
                    event_type = event.type

                    if event_type == "message_start":
                        msg = event.message
                        collected_model = msg.model
                        if msg.usage:
                            usage["input_tokens"] = msg.usage.input_tokens

                    elif event_type == "content_block_start":
                        block = event.content_block
                        if block.type == "tool_use":
                            current_tool = {
                                "id": block.id,
                                "type": "function",
                                "function": {
                                    "name": block.name,
                                    "arguments": "",
                                },
                            }

                    elif event_type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            yield (delta.text, None)
                        elif delta.type == "input_json_delta" and current_tool is not None:
                            current_tool["function"]["arguments"] += delta.partial_json

                    elif event_type == "content_block_stop":
                        if current_tool is not None:
                            tool_calls.append(current_tool)
                            current_tool = None

                    elif event_type == "message_delta":
                        if event.delta.stop_reason:
                            finish_reason = event.delta.stop_reason
                        if event.usage:
                            usage["output_tokens"] = event.usage.output_tokens

                    elif event_type == "message_stop":
                        pass  # Stream complete

        except Exception as e:
            raise BackendError(
                f"Anthropic streaming failed: {e}",
                backend="anthropic_native",
            ) from e

        elapsed_ms = (time.perf_counter() - start_time) * 1000

        # Compute total tokens
        usage["prompt_tokens"] = usage.get("input_tokens", 0)
        usage["completion_tokens"] = usage.get("output_tokens", 0)
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]

        # Map Anthropic finish reasons to OpenAI-compatible ones
        mapped_finish = self._map_finish_reason(finish_reason)

        yield (
            "",
            {
                "model": collected_model,
                "usage": usage,
                "latency_ms": round(elapsed_ms, 1),
                "finish_reason": mapped_finish,
                "tool_calls": tool_calls,
            },
        )

    # ------------------------------------------------------------------
    # CAS persistence (shared pattern with CASOpenAIBackend)
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
        request_result = self.write_content(request_bytes, context=context)

        response_payload = {
            "model": model,
            "content": response_content,
            "finish_reason": finish_reason,
            "usage": usage,
            "latency_ms": round(latency_ms, 1),
        }
        response_json = json.dumps(response_payload, separators=(",", ":")).encode("utf-8")
        response_result = self.write_content(response_json, context=context)

        session = {
            "type": "llm_session_v1",
            "request_hash": request_result.content_id,
            "response_hash": response_result.content_id,
            "model": model,
            "latency_ms": round(latency_ms, 1),
        }
        session_json = json.dumps(session, separators=(",", ":")).encode("utf-8")
        return self.write_content(session_json, context=context)

    @property
    def active_streams(self) -> list[str]:
        return list(self._active_tasks.keys())

    async def shutdown_streams(self) -> None:
        paths = list(self._active_tasks.keys())
        for path in paths:
            await self.cancel_stream(path)

    # ------------------------------------------------------------------
    # Format conversion helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _convert_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert OpenAI-format messages to Anthropic format.

        Key differences:
        - Anthropic doesn't have 'system' role in messages (it's top-level)
        - tool results use type: "tool_result" content block, not role: "tool"
        """
        converted = []
        for msg in messages:
            role = msg.get("role", "")
            if role == "system":
                continue  # Handled as top-level 'system' param

            if role == "tool":
                # OpenAI: {"role": "tool", "content": "...", "tool_call_id": "..."}
                # Anthropic: {"role": "user", "content": [{"type": "tool_result", ...}]}
                converted.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.get("tool_call_id", ""),
                                "content": msg.get("content", ""),
                            }
                        ],
                    }
                )
                continue

            entry: dict[str, Any] = {"role": role}

            # Handle assistant messages with tool_calls
            if role == "assistant" and msg.get("tool_calls"):
                content_blocks: list[dict[str, Any]] = []
                text_content = msg.get("content")
                if text_content:
                    content_blocks.append({"type": "text", "text": text_content})
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    try:
                        input_data = json.loads(func.get("arguments", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        input_data = {}
                    content_blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": func.get("name", ""),
                            "input": input_data,
                        }
                    )
                entry["content"] = content_blocks
            else:
                entry["content"] = msg.get("content", "")

            converted.append(entry)

        # Anthropic requires first message to be from user
        if converted and converted[0].get("role") != "user":
            converted.insert(0, {"role": "user", "content": "Continue."})

        return converted

    @staticmethod
    def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert OpenAI-format tools to Anthropic format.

        OpenAI:  {"type": "function", "function": {"name": ..., "parameters": ...}}
        Anthropic: {"name": ..., "input_schema": ...}
        """
        return [
            {
                "name": t.get("function", {}).get("name", ""),
                "description": t.get("function", {}).get("description", ""),
                "input_schema": t.get("function", {}).get("parameters", {"type": "object"}),
            }
            for t in tools
            if t.get("type") == "function"
        ]

    @staticmethod
    def _map_finish_reason(reason: str | None) -> str:
        """Map Anthropic stop reasons to OpenAI-compatible ones."""
        mapping = {
            "end_turn": "stop",
            "tool_use": "tool_calls",
            "max_tokens": "length",
            "stop_sequence": "stop",
        }
        return mapping.get(reason or "", reason or "stop")
