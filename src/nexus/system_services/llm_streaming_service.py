"""LLM streaming service — DT_STREAM orchestration for LLM token delivery.

Bridges kernel IPC (StreamManager / DT_STREAM) and backend compute
(OpenAICompatibleBackend), following the DT_PIPE consumer pattern
established by WorkflowDispatchService and TaskDispatchPipeConsumer.

Architecture boundaries:
    - Backend never imports/touches StreamManager (pure compute + CAS)
    - Kernel never parses LLM request JSON or knows about streaming
    - Service orchestrates both, bridging tokens from backend → DT_STREAM

Streaming flow:
    1. start_stream(request_bytes, stream_path)
       → creates DT_STREAM, spawns background task, returns immediately
    2. _run_stream() background task:
       → runs backend.generate_streaming() in thread (sync OpenAI SDK)
       → each token pushed to DT_STREAM via queue bridge (μs, heap)
       → after streaming: collect_all() → persist_session() (CAS)
       → signal_close() → readers notified

DI dependencies (no god-object access):
    - stream_manager: StreamManager for kernel DT_STREAM lifecycle
    - backend: OpenAICompatibleBackend for LLM compute + CAS persist

References:
    - Task #1589: LLM backend driver design
    - WorkflowDispatchService: DT_PIPE consumer pattern precedent
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import queue
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from nexus.backends.compute.openai_compatible import OpenAICompatibleBackend
    from nexus.core.stream_manager import StreamManager

logger = logging.getLogger(__name__)

# Default stream capacity: 8MB for LLM responses
_DEFAULT_STREAM_CAPACITY = 8 * 1024 * 1024


class LLMStreamingService:
    """Orchestrates LLM streaming via DT_STREAM + CAS persistence.

    Service-layer bridge between kernel IPC (StreamManager) and
    backend compute (OpenAICompatibleBackend). Backend produces tokens
    via a sync generator; this service pushes them to DT_STREAM for
    real-time fan-out, then flushes to CAS after streaming completes.

    DI pattern follows WorkflowDispatchService: constructor injection
    for stream_manager + backend, late injection not needed.
    """

    def __init__(
        self,
        *,
        stream_manager: "StreamManager",
        backend: "OpenAICompatibleBackend",
    ) -> None:
        self._stream_manager = stream_manager
        self._backend = backend
        self._active_tasks: dict[str, asyncio.Task[None]] = {}

    async def start_stream(
        self,
        request_bytes: bytes,
        stream_path: str,
        *,
        capacity: int = _DEFAULT_STREAM_CAPACITY,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        """Start a streaming LLM call, delivering tokens via DT_STREAM.

        Creates a DT_STREAM at ``stream_path``, spawns a background task
        that iterates the backend's sync generator and pushes tokens to
        the stream. Returns immediately — callers read tokens via
        ``sys_read(stream_path, offset=N)``.

        Args:
            request_bytes: JSON-encoded request with ``messages`` field.
            stream_path: VFS path for the DT_STREAM (e.g.
                ``/zone/llm/.streams/sid-123``).
            capacity: Stream buffer byte capacity (default 8MB).
            owner_id: Owner for ReBAC permission checks.

        Returns:
            ``{"stream_path": ..., "status": "streaming"}``

        Raises:
            BackendError: If StreamManager is not available.
        """
        self._stream_manager.create(stream_path, capacity=capacity, owner_id=owner_id)

        task = asyncio.create_task(
            self._run_stream(request_bytes, stream_path),
            name=f"llm-stream:{stream_path}",
        )
        self._active_tasks[stream_path] = task

        return {"stream_path": stream_path, "status": "streaming"}

    async def cancel_stream(self, stream_path: str) -> bool:
        """Cancel an active streaming LLM call.

        Cancels the background task and destroys the DT_STREAM.

        Returns:
            True if the stream was active and cancelled, False otherwise.
        """
        task = self._active_tasks.pop(stream_path, None)
        if task is None:
            return False

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        with contextlib.suppress(Exception):
            self._stream_manager.destroy(stream_path)

        logger.info("LLM stream cancelled: %s", stream_path)
        return True

    # ------------------------------------------------------------------
    # Background streaming task
    # ------------------------------------------------------------------

    async def _run_stream(self, request_bytes: bytes, stream_path: str) -> None:
        """Background task: pump tokens from LLM to DT_STREAM, then CAS persist.

        Uses a ``queue.Queue`` to bridge the sync generator (running in
        a thread) to the async event loop. The consumer reads from the
        queue and pushes tokens to DT_STREAM via ``stream_write_nowait()``.
        """
        sm = self._stream_manager

        # Thread-safe queue bridging sync generator → async consumer
        _SENTINEL: object = object()
        token_q: queue.Queue[tuple[str, dict[str, Any] | None] | object | Exception] = queue.Queue(
            maxsize=4096
        )

        def _producer() -> None:
            """Thread: iterate sync generator, push to queue."""
            try:
                request = json.loads(request_bytes)
                for item in self._backend.generate_streaming(request):
                    token_q.put(item)
                token_q.put(_SENTINEL)
            except Exception as exc:
                token_q.put(exc)

        loop = asyncio.get_running_loop()
        producer_fut = loop.run_in_executor(None, _producer)

        meta: dict[str, Any] = {}
        try:
            # Consume tokens from queue, push to DT_STREAM
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
                    sm.stream_write_nowait(stream_path, token.encode("utf-8"))
                if token_meta is not None:
                    meta = token_meta

            # CAS persist: collect all tokens → build session envelope
            full_response = sm.collect_all(stream_path)
            result = self._backend.persist_session(
                request_bytes=request_bytes,
                response_content=full_response.decode("utf-8"),
                model=meta.get("model", ""),
                finish_reason="stop",
                usage=meta.get("usage", {}),
                latency_ms=meta.get("latency_ms", 0),
            )

            # Notify readers: streaming done + session hash for CAS replay
            done_msg = json.dumps(
                {
                    "type": "done",
                    "session_hash": result.content_id,
                    "model": meta.get("model", ""),
                    "latency_ms": meta.get("latency_ms", 0),
                },
                separators=(",", ":"),
            )
            sm.stream_write_nowait(stream_path, done_msg.encode("utf-8"))
            sm.signal_close(stream_path)

            logger.info(
                "LLM stream completed: %s model=%s session=%s",
                stream_path,
                meta.get("model", ""),
                result.content_id[:16],
            )

        except asyncio.CancelledError:
            logger.info("LLM stream cancelled: %s", stream_path)
            with contextlib.suppress(Exception):
                sm.signal_close(stream_path)
            raise

        except Exception as exc:
            logger.error("LLM stream failed: %s error=%s", stream_path, exc)
            # Write error to stream so readers know what happened
            error_msg = json.dumps(
                {"type": "error", "message": str(exc)},
                separators=(",", ":"),
            )
            with contextlib.suppress(Exception):
                sm.stream_write_nowait(stream_path, error_msg.encode("utf-8"))
                sm.signal_close(stream_path)

        finally:
            # Ensure producer thread completes
            with contextlib.suppress(Exception):
                await producer_fut
            self._active_tasks.pop(stream_path, None)

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    @property
    def active_streams(self) -> list[str]:
        """Return VFS paths of currently active streaming LLM calls."""
        return list(self._active_tasks.keys())

    async def shutdown(self) -> None:
        """Cancel all active streams. Called on kernel shutdown."""
        paths = list(self._active_tasks.keys())
        for path in paths:
            await self.cancel_stream(path)
        logger.info("LLM streaming service shut down (%d streams cancelled)", len(paths))
