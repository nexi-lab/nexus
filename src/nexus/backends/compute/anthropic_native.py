"""Anthropic-native LLM backend — CAS addressing + Claude API.

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

References:
    - Task #1589: LLM backend driver design
    - docs/architecture/nexus-agent-plan.md §1.2
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
    """CAS addressing + native Anthropic Claude SDK.

    Uses the Anthropic Python SDK directly for:
    - Native tool_use content blocks (complete JSON, no incremental concatenation)
    - Native content_block_start/delta/stop streaming events
    - Native extended_thinking support
    - Native prompt caching (cache_control)

    generate_streaming() yields CC-format content block frames directly.
    ManagedAgentLoop iterates the generator synchronously.
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

        from nexus.backends.compute.message_chunking import MessageBoundaryStrategy
        from nexus.backends.engines.cdc import ChunkingStrategy

        cdc: ChunkingStrategy = MessageBoundaryStrategy(self)
        self._cdc = cdc

    @property
    def name(self) -> str:
        return "anthropic_native"

    def set_stream_manager(self, nx_or_sm: Any) -> None:
        """No-op — DT_STREAM orchestration removed. Kept for factory compatibility."""

    # ------------------------------------------------------------------
    # Streaming — CC-format content block frames
    # ------------------------------------------------------------------

    def generate_streaming(self, request: dict[str, Any]) -> Iterator[dict]:
        """Yield CC-format content block frames from Anthropic streaming API.

        Each yielded dict is a CC content block or metadata frame:
            {"type": "text", "text": "..."}
            {"type": "thinking", "thinking": "..."}
            {"type": "signature", "signature": "..."}
            {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
            {"type": "server_tool_use", "id": "...", "name": "...", "input": {...}}
            {"type": "usage", "usage": {...}}
            {"type": "stop", "stop_reason": "..."}
            {"type": "error", "message": "..."}

        Args:
            request: Dict with ``messages`` and optional ``model``, ``system``,
                ``max_tokens``, ``tools``, ``temperature``, etc.
        """
        if "messages" not in request:
            raise BackendError(
                "Request must contain 'messages' field",
                backend="anthropic_native",
            )

        model = request.get("model", self._default_model)
        messages = request["messages"]
        max_tokens = request.get("max_tokens", 8192)

        api_kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._convert_messages(messages),
            "max_tokens": max_tokens,
        }

        # System prompt (Anthropic uses top-level 'system', not a message)
        system = request.get("system")
        if not system:
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

        usage: dict[str, int] = {}
        stop_reason: str | None = None
        current_tool: dict[str, Any] | None = None

        try:
            with self._client.messages.stream(**api_kwargs) as stream:
                for event in stream:
                    event_type = event.type

                    if event_type == "message_start":
                        msg = event.message
                        if msg.usage:
                            usage["input_tokens"] = msg.usage.input_tokens
                            usage["cache_creation_input_tokens"] = getattr(
                                msg.usage, "cache_creation_input_tokens", 0
                            )
                            usage["cache_read_input_tokens"] = getattr(
                                msg.usage, "cache_read_input_tokens", 0
                            )

                    elif event_type == "content_block_start":
                        block = event.content_block
                        if block.type == "tool_use":
                            current_tool = {
                                "type": "tool_use",
                                "id": block.id,
                                "name": block.name,
                                "input": "",
                            }
                        elif block.type == "server_tool_use":
                            current_tool = {
                                "type": "server_tool_use",
                                "id": getattr(block, "id", ""),
                                "name": getattr(block, "name", ""),
                                "input": "",
                            }

                    elif event_type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            yield {"type": "text", "text": delta.text}
                        elif delta.type == "thinking_delta":
                            yield {"type": "thinking", "thinking": delta.thinking}
                        elif delta.type == "input_json_delta" and current_tool is not None:
                            current_tool["input"] += delta.partial_json
                        elif delta.type == "signature_delta":
                            yield {"type": "signature", "signature": delta.signature}

                    elif event_type == "content_block_stop":
                        if current_tool is not None:
                            if isinstance(current_tool["input"], str):
                                try:
                                    current_tool["input"] = json.loads(current_tool["input"])
                                except (json.JSONDecodeError, TypeError):
                                    current_tool["input"] = {}
                            yield current_tool
                            current_tool = None

                    elif event_type == "message_delta":
                        if event.delta.stop_reason:
                            stop_reason = event.delta.stop_reason
                        if event.usage:
                            usage["output_tokens"] = event.usage.output_tokens

        except Exception as e:
            yield {"type": "error", "message": f"Anthropic streaming failed: {e}"}
            return

        # Compute totals
        usage["prompt_tokens"] = usage.get("input_tokens", 0)
        usage["completion_tokens"] = usage.get("output_tokens", 0)
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]

        yield {"type": "usage", "usage": usage}
        yield {"type": "stop", "stop_reason": self._map_finish_reason(stop_reason)}

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
