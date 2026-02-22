"""RLM inference service — orchestrates recursive language model inference.

This is the main entry point for RLM inference. It manages:
1. Dedicated thread pool (prevents starving other endpoints)
2. NexusREPL environment (wraps SandboxManager)
3. NexusLMClient (wraps LiteLLMProvider)
4. Budget guardrails (iterations, duration, tokens)
5. SSE event streaming (per-iteration progress)

The RLM algorithm:
  1. Create sandbox, inject tools (nexus_read, nexus_search, FINAL)
  2. Load context metadata (query + paths — NOT file content)
  3. System prompt instructs model to write Python code to analyze context
  4. Loop: model generates code → sandbox executes → output shown to model
  5. When model calls FINAL("answer") → return answer
  6. If budget exceeded → return partial results

Architecture Decisions:
  - Issue 2B: run_in_executor (sync RLM loop in thread pool)
  - Issue 3B: Custom LM client wrapping LiteLLMProvider
  - Issue 4C: SSE streaming (per-iteration events)
  - Issue 7A: Service-level guardrails
  - Issue 8A: Structured error categories
  - Issue 13A: Dedicated thread pool (max_workers=8)

Reference: arXiv:2512.24601 (Zhang, Kraska, Khattab — MIT OASYS Lab)
"""

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from nexus.bricks.rlm.environment import NexusREPL
from nexus.bricks.rlm.lm_client import NexusLMClient
from nexus.bricks.rlm.types import (
    REPLResult,
    RLMInferenceRequest,
    RLMInferenceResult,
    RLMInfrastructureError,
    RLMIteration,
    RLMStatus,
    SSEEvent,
    SSEEventType,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt for RLM
# ---------------------------------------------------------------------------

_RLM_SYSTEM_PROMPT = """\
You are an advanced reasoning agent with access to a Python REPL environment.
Your task is to answer a question by programmatically analyzing data stored in Nexus VFS.

## Available Tools (pre-loaded in your REPL)

- `nexus_read(path)` — Read a file from Nexus. Returns file content as string.
- `nexus_search(query, limit=10, mode="hybrid")` — Search Nexus for relevant files.
- `nexus_list(path="/")` — List directory contents.
- `FINAL(answer)` — Call this when you have your final answer.
- `FINAL_VAR(var_name)` — Use a variable as your final answer.

## How to Work

1. Start by understanding the available data: use `nexus_list()` and `nexus_search()`.
2. Read specific files with `nexus_read(path)` to inspect content.
3. Write Python code to analyze, filter, and reason about the data.
4. When you have your answer, call `FINAL("your answer here")`.

## Rules

- Write Python code in ```python blocks.
- Each code block is executed in a stateful Jupyter kernel (variables persist).
- REPL output is truncated to 20K characters. Use programmatic access, not bulk printing.
- You can make multiple iterations — take your time to reason carefully.
- If you need more information, search for it or read more files.
"""

# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def _extract_code_blocks(text: str) -> list[str]:
    """Extract Python code blocks from an LLM response.

    Supports ```python and ``` (generic) fenced code blocks.

    Args:
        text: LLM response text.

    Returns:
        List of code strings extracted from code blocks.
    """
    pattern = r"```(?:python)?\s*\n(.*?)```"
    blocks = re.findall(pattern, text, re.DOTALL)
    return [block.strip() for block in blocks if block.strip()]


def _find_final_answer(output: str) -> str | None:
    """Detect if REPL output contains a FINAL ANSWER marker.

    Args:
        output: REPL stdout to scan.

    Returns:
        The final answer string, or None if not found.
    """
    match = re.search(r"FINAL ANSWER:\s*(.+?)(?:\n|$)", output)
    if match:
        return match.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class RLMInferenceService:
    """Orchestrates RLM inference with sandbox code execution.

    Uses a dedicated thread pool to prevent RLM from starving other
    FastAPI endpoints. Each inference job gets its own sandbox and
    LM client instance.

    Args:
        sandbox_manager: Nexus SandboxManager for code execution.
        llm_provider: LiteLLMProvider for LLM calls.
        nexus_api_url: Nexus REST API URL (for REPL tools to call back).
        max_concurrent: Max concurrent RLM jobs (thread pool size).
    """

    def __init__(
        self,
        sandbox_manager: Any,
        llm_provider: Any,
        nexus_api_url: str,
        max_concurrent: int = 8,
    ) -> None:
        self._sandbox_manager = sandbox_manager
        self._llm_provider = llm_provider
        self._nexus_api_url = nexus_api_url
        self._executor = ThreadPoolExecutor(
            max_workers=max_concurrent,
            thread_name_prefix="rlm",
        )

    async def infer(
        self,
        request: RLMInferenceRequest,
        *,
        user_id: str,
        api_key: str,
    ) -> RLMInferenceResult:
        """Run RLM inference (non-streaming).

        Runs the entire inference loop in a thread pool executor and
        returns the final result.

        Args:
            request: Inference request parameters.
            user_id: Authenticated user ID.
            api_key: API key for REPL tools to call back to Nexus.

        Returns:
            RLMInferenceResult with status, answer, and iteration details.
        """
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                self._executor,
                self._run_inference,
                request,
                user_id,
                api_key,
            )
        except RLMInfrastructureError as exc:
            return RLMInferenceResult(
                status=RLMStatus.FAILED,
                error_message=str(exc),
            )

    async def infer_stream(
        self,
        request: RLMInferenceRequest,
        *,
        user_id: str,
        api_key: str,
    ) -> AsyncIterator[SSEEvent]:
        """Run RLM inference with SSE streaming.

        Yields SSE events for each iteration, providing real-time
        progress to the client.

        Args:
            request: Inference request parameters.
            user_id: Authenticated user ID.
            api_key: API key for REPL tools to call back to Nexus.

        Yields:
            SSEEvent objects (started, iteration, final_answer, error).
        """
        yield SSEEvent(
            event=SSEEventType.STARTED,
            data={
                "query": request.query,
                "max_iterations": request.max_iterations,
                "model": request.model,
            },
        )

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                self._executor,
                self._run_inference,
                request,
                user_id,
                api_key,
            )

            # Yield iteration events
            for iteration in result.iterations:
                yield SSEEvent(
                    event=SSEEventType.ITERATION,
                    data={
                        "step": iteration.step,
                        "code_executed": iteration.code_executed[:500],
                        "output_summary": iteration.repl_result.stdout[:500],
                        "tokens_used": iteration.tokens_used,
                        "duration_seconds": iteration.duration_seconds,
                    },
                )

            # Yield final event
            if result.status == RLMStatus.COMPLETED:
                yield SSEEvent(
                    event=SSEEventType.FINAL_ANSWER,
                    data={
                        "answer": result.answer or "",
                        "total_tokens": result.total_tokens,
                        "total_duration_seconds": result.total_duration_seconds,
                        "iterations": len(result.iterations),
                    },
                )
            elif result.status == RLMStatus.BUDGET_EXCEEDED:
                yield SSEEvent(
                    event=SSEEventType.BUDGET_EXCEEDED,
                    data={
                        "reason": result.error_message or "budget exceeded",
                        "total_tokens": result.total_tokens,
                        "iterations": len(result.iterations),
                    },
                )
            else:
                yield SSEEvent(
                    event=SSEEventType.ERROR,
                    data={"error": result.error_message or "unknown error"},
                )

        except Exception as exc:
            yield SSEEvent(
                event=SSEEventType.ERROR,
                data={"error": str(exc)},
            )

    def _run_inference(
        self,
        request: RLMInferenceRequest,
        user_id: str,
        api_key: str,
    ) -> RLMInferenceResult:
        """Run the RLM inference loop (synchronous, runs in thread pool).

        This is the core algorithm:
        1. Create sandbox + inject tools
        2. Load context metadata
        3. Loop: prompt LLM → extract code → execute → check for FINAL
        4. Return result

        Args:
            request: Inference request.
            user_id: User ID.
            api_key: API key for tools.

        Returns:
            RLMInferenceResult with status and answer.
        """
        start_time = time.monotonic()
        iterations: list[RLMIteration] = []
        total_tokens = 0

        # Create REPL environment
        repl = NexusREPL(
            sandbox_manager=self._sandbox_manager,
            user_id=user_id,
            zone_id=request.zone_id,
            nexus_api_url=self._nexus_api_url,
            nexus_api_key=api_key,
            sandbox_provider=request.sandbox_provider,
        )

        try:
            # Setup sandbox + tools
            repl.setup()

            # Load context metadata (query + paths, NOT file content)
            context_payload = {
                "query": request.query,
                "paths": list(request.context_paths),
                "instructions": (
                    "Use nexus_read(path) to read files and nexus_search(query) "
                    "to search. Call FINAL(answer) when done."
                ),
            }
            repl.load_context(context_payload)

            # Create LM client
            lm_client = NexusLMClient(
                provider=self._llm_provider,
                model=request.model,
            )

            # Build initial message history
            messages: list[dict[str, str]] = [
                {"role": "system", "content": _RLM_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": self._build_user_prompt(request),
                },
            ]

            # Iteration loop
            for step in range(1, request.max_iterations + 1):
                iter_start = time.monotonic()

                # Budget check: duration
                elapsed = time.monotonic() - start_time
                if elapsed > request.max_duration_seconds:
                    return RLMInferenceResult(
                        status=RLMStatus.BUDGET_EXCEEDED,
                        iterations=tuple(iterations),
                        total_tokens=total_tokens,
                        total_duration_seconds=elapsed,
                        error_message=f"Duration limit exceeded: {elapsed:.1f}s > {request.max_duration_seconds}s",
                    )

                # Budget check: tokens
                if total_tokens > request.max_total_tokens:
                    return RLMInferenceResult(
                        status=RLMStatus.BUDGET_EXCEEDED,
                        iterations=tuple(iterations),
                        total_tokens=total_tokens,
                        total_duration_seconds=time.monotonic() - start_time,
                        error_message=f"Token limit exceeded: {total_tokens} > {request.max_total_tokens}",
                    )

                # Call LLM
                response_text = lm_client.chat(messages, model=request.model)
                total_tokens = lm_client.total_tokens_used

                # Extract code blocks
                code_blocks = _extract_code_blocks(response_text)

                if not code_blocks:
                    # No code — model is thinking/explaining
                    # Add response to history and continue
                    messages.append({"role": "assistant", "content": response_text})
                    messages.append(
                        {
                            "role": "user",
                            "content": "Please write Python code to continue your analysis. "
                            "Use nexus_read(), nexus_search(), and FINAL() when done.",
                        }
                    )
                    iterations.append(
                        RLMIteration(
                            step=step,
                            code_executed="(no code generated)",
                            repl_result=REPLResult(),
                            tokens_used=total_tokens,
                            duration_seconds=time.monotonic() - iter_start,
                        )
                    )
                    continue

                # Execute each code block
                combined_stdout = ""
                combined_stderr = ""
                combined_code = "\n".join(code_blocks)

                repl_result = repl.execute_code(combined_code)
                combined_stdout += repl_result.stdout
                combined_stderr += repl_result.stderr

                # Record iteration
                iteration = RLMIteration(
                    step=step,
                    code_executed=combined_code,
                    repl_result=repl_result,
                    tokens_used=total_tokens,
                    duration_seconds=time.monotonic() - iter_start,
                )
                iterations.append(iteration)

                # Check for FINAL answer
                final_answer = _find_final_answer(combined_stdout)
                if final_answer is not None:
                    return RLMInferenceResult(
                        status=RLMStatus.COMPLETED,
                        answer=final_answer,
                        iterations=tuple(iterations),
                        total_tokens=total_tokens,
                        total_duration_seconds=time.monotonic() - start_time,
                    )

                # Build next prompt with REPL output
                output_summary = combined_stdout[:8192] if combined_stdout else "(no output)"
                error_msg = f"\nErrors:\n{combined_stderr[:2000]}" if combined_stderr else ""

                messages.append({"role": "assistant", "content": response_text})
                messages.append(
                    {
                        "role": "user",
                        "content": f"REPL Output:\n{output_summary}{error_msg}\n\n"
                        f"Continue your analysis. Step {step}/{request.max_iterations}. "
                        f"Call FINAL(answer) when you have your answer.",
                    }
                )

            # Exhausted iterations without FINAL
            return RLMInferenceResult(
                status=RLMStatus.BUDGET_EXCEEDED,
                iterations=tuple(iterations),
                total_tokens=total_tokens,
                total_duration_seconds=time.monotonic() - start_time,
                error_message=f"Max iterations reached: {request.max_iterations}",
            )

        except RLMInfrastructureError as exc:
            return RLMInferenceResult(
                status=RLMStatus.FAILED,
                iterations=tuple(iterations),
                total_tokens=total_tokens,
                total_duration_seconds=time.monotonic() - start_time,
                error_message=str(exc),
            )

        finally:
            repl.cleanup()

    def _build_user_prompt(self, request: RLMInferenceRequest) -> str:
        """Build the initial user prompt for the RLM."""
        parts = [f"Question: {request.query}"]
        if request.context_paths:
            paths_list = "\n".join(f"  - {p}" for p in request.context_paths)
            parts.append(f"\nRelevant files (use nexus_read to examine):\n{paths_list}")
        parts.append(
            "\nStart by exploring the available data, then analyze it to answer the question. "
            "Call FINAL(answer) when you have your answer."
        )
        return "\n".join(parts)

    def shutdown(self) -> None:
        """Shutdown the thread pool executor."""
        self._executor.shutdown(wait=False)
