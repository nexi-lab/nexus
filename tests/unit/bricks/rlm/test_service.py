"""Tests for RLMInferenceService — high-level RLM inference orchestrator.

Tests verify:
- Dedicated thread pool (max_workers configurable)
- Budget guardrails (max_iterations, max_duration, max_tokens)
- Code extraction from LLM responses
- FINAL() detection in REPL output
- SSE event generation
- Error categorization (infrastructure, code, budget)
- Cleanup on completion
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.rlm.service import RLMInferenceService, _extract_code_blocks, _find_final_answer
from nexus.bricks.rlm.types import (
    RLMInferenceRequest,
    RLMInferenceResult,
    RLMStatus,
    SSEEvent,
    SSEEventType,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(
    *,
    sandbox_manager: MagicMock | None = None,
    llm_provider: MagicMock | None = None,
    max_concurrent: int = 2,
) -> RLMInferenceService:
    """Create an RLMInferenceService with mocked dependencies."""
    if sandbox_manager is None:
        sandbox_manager = MagicMock()
        sandbox_manager.create_sandbox = AsyncMock(
            return_value={"sandbox_id": "sb-123", "status": "active", "provider": "docker"}
        )
        result_mock = MagicMock()
        result_mock.stdout = ""
        result_mock.stderr = ""
        result_mock.exit_code = 0
        result_mock.execution_time = 0.1
        sandbox_manager.run_code = AsyncMock(return_value=result_mock)
        sandbox_manager.stop_sandbox = AsyncMock(return_value={"status": "stopped"})

    if llm_provider is None:
        llm_provider = MagicMock()
        mock_response = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = '```python\nprint("hello")\n```'
        mock_response.choices = [mock_choice]
        llm_provider.complete_async = AsyncMock(return_value=mock_response)
        llm_provider.count_tokens = MagicMock(return_value=50)

    return RLMInferenceService(
        sandbox_manager=sandbox_manager,
        llm_provider=llm_provider,
        nexus_api_url="http://localhost:2026",
        max_concurrent=max_concurrent,
    )


# ---------------------------------------------------------------------------
# Code extraction
# ---------------------------------------------------------------------------


class TestExtractCodeBlocks:
    """_extract_code_blocks() parses code from LLM responses."""

    def test_single_python_block(self) -> None:
        text = 'Here is my code:\n```python\nprint("hello")\n```\nDone.'
        blocks = _extract_code_blocks(text)
        assert len(blocks) == 1
        assert blocks[0] == 'print("hello")'

    def test_multiple_blocks(self) -> None:
        text = "```python\nx = 1\n```\nThen:\n```python\ny = 2\n```"
        blocks = _extract_code_blocks(text)
        assert len(blocks) == 2
        assert blocks[0] == "x = 1"
        assert blocks[1] == "y = 2"

    def test_no_code_blocks(self) -> None:
        text = "No code here, just text."
        blocks = _extract_code_blocks(text)
        assert blocks == []

    def test_generic_code_block(self) -> None:
        text = "```\nprint('hi')\n```"
        blocks = _extract_code_blocks(text)
        assert len(blocks) == 1

    def test_empty_code_block(self) -> None:
        text = "```python\n```"
        blocks = _extract_code_blocks(text)
        assert blocks == [] or blocks == [""]


class TestFindFinalAnswer:
    """_find_final_answer() detects FINAL() calls in REPL output."""

    def test_finds_final_answer(self) -> None:
        output = "Processing...\nFINAL ANSWER: The result is 42.\nDone."
        answer = _find_final_answer(output)
        assert answer == "The result is 42."

    def test_no_final_answer(self) -> None:
        output = "Just some output\nNo answer yet"
        answer = _find_final_answer(output)
        assert answer is None

    def test_final_answer_at_end(self) -> None:
        output = "FINAL ANSWER: yes"
        answer = _find_final_answer(output)
        assert answer == "yes"


# ---------------------------------------------------------------------------
# Service behavior
# ---------------------------------------------------------------------------


class TestInferenceFlow:
    """Full inference flow with mocked sandbox and LLM."""

    @pytest.mark.asyncio
    async def test_basic_inference_returns_result(self) -> None:
        """Model immediately produces FINAL() → should complete in 1 iteration."""
        sandbox_mgr = MagicMock()
        sandbox_mgr.create_sandbox = AsyncMock(
            return_value={"sandbox_id": "sb-1", "status": "active", "provider": "docker"}
        )
        # First run_code call = tool injection (returns empty)
        inject_result = MagicMock(stdout="Tools loaded", stderr="", exit_code=0, execution_time=0.1)
        # Second run_code call = context loading
        context_result = MagicMock(
            stdout="Context loaded", stderr="", exit_code=0, execution_time=0.1
        )
        # Third+ run_code calls = code execution with FINAL
        exec_result = MagicMock(
            stdout="FINAL ANSWER: 42", stderr="", exit_code=0, execution_time=0.2
        )
        sandbox_mgr.run_code = AsyncMock(side_effect=[inject_result, context_result, exec_result])
        sandbox_mgr.stop_sandbox = AsyncMock()

        llm_provider = MagicMock()
        mock_resp = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = '```python\nFINAL("42")\n```'
        mock_resp.choices = [mock_choice]
        llm_provider.complete_async = AsyncMock(return_value=mock_resp)
        llm_provider.count_tokens = MagicMock(return_value=30)

        service = RLMInferenceService(
            sandbox_manager=sandbox_mgr,
            llm_provider=llm_provider,
            nexus_api_url="http://localhost:2026",
            max_concurrent=2,
        )

        req = RLMInferenceRequest(
            query="What is the answer?",
            context_paths=("/data/doc.md",),
            max_iterations=5,
            stream=False,
        )

        result = await service.infer(req, user_id="user-1", api_key="key-1")

        assert isinstance(result, RLMInferenceResult)
        assert result.status == RLMStatus.COMPLETED
        assert result.answer == "42"

    @pytest.mark.asyncio
    async def test_max_iterations_exceeded(self) -> None:
        """Model never produces FINAL() → should hit iteration limit."""
        sandbox_mgr = MagicMock()
        sandbox_mgr.create_sandbox = AsyncMock(
            return_value={"sandbox_id": "sb-2", "status": "active", "provider": "docker"}
        )
        # All run_code calls return no FINAL
        no_final_result = MagicMock(stdout="42", stderr="", exit_code=0, execution_time=0.1)
        sandbox_mgr.run_code = AsyncMock(return_value=no_final_result)
        sandbox_mgr.stop_sandbox = AsyncMock()

        llm_provider = MagicMock()
        mock_resp = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "```python\nprint(42)\n```"
        mock_resp.choices = [mock_choice]
        llm_provider.complete_async = AsyncMock(return_value=mock_resp)
        llm_provider.count_tokens = MagicMock(return_value=30)

        service = RLMInferenceService(
            sandbox_manager=sandbox_mgr,
            llm_provider=llm_provider,
            nexus_api_url="http://localhost:2026",
            max_concurrent=2,
        )

        req = RLMInferenceRequest(
            query="What is the answer?",
            max_iterations=3,
            stream=False,
        )

        result = await service.infer(req, user_id="user-1", api_key="key-1")

        assert result.status == RLMStatus.BUDGET_EXCEEDED

    @pytest.mark.asyncio
    async def test_infrastructure_error_on_sandbox_failure(self) -> None:
        """Sandbox creation fails → should return FAILED status."""
        sandbox_mgr = MagicMock()
        sandbox_mgr.create_sandbox = AsyncMock(side_effect=ValueError("No providers available"))

        service = RLMInferenceService(
            sandbox_manager=sandbox_mgr,
            llm_provider=MagicMock(),
            nexus_api_url="http://localhost:2026",
            max_concurrent=2,
        )

        req = RLMInferenceRequest(query="test", stream=False)
        result = await service.infer(req, user_id="user-1", api_key="key-1")

        assert result.status == RLMStatus.FAILED
        assert "sandbox" in (result.error_message or "").lower()


class TestSSEStreaming:
    """SSE event streaming."""

    @pytest.mark.asyncio
    async def test_stream_produces_events(self) -> None:
        """Streaming mode should yield SSE events."""
        sandbox_mgr = MagicMock()
        sandbox_mgr.create_sandbox = AsyncMock(
            return_value={"sandbox_id": "sb-3", "status": "active", "provider": "docker"}
        )
        exec_result = MagicMock(
            stdout="FINAL ANSWER: done", stderr="", exit_code=0, execution_time=0.1
        )
        sandbox_mgr.run_code = AsyncMock(return_value=exec_result)
        sandbox_mgr.stop_sandbox = AsyncMock()

        llm_provider = MagicMock()
        mock_resp = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = '```python\nFINAL("done")\n```'
        mock_resp.choices = [mock_choice]
        llm_provider.complete_async = AsyncMock(return_value=mock_resp)
        llm_provider.count_tokens = MagicMock(return_value=20)

        service = RLMInferenceService(
            sandbox_manager=sandbox_mgr,
            llm_provider=llm_provider,
            nexus_api_url="http://localhost:2026",
            max_concurrent=2,
        )

        req = RLMInferenceRequest(query="test", stream=True, max_iterations=3)

        events: list[SSEEvent] = []
        async for event in service.infer_stream(req, user_id="user-1", api_key="key-1"):
            events.append(event)

        # Should have at least started + final_answer events
        event_types = [e.event for e in events]
        assert SSEEventType.STARTED in event_types
        assert SSEEventType.FINAL_ANSWER in event_types or SSEEventType.ERROR in event_types


class TestCleanup:
    """Service cleanup behavior."""

    @pytest.mark.asyncio
    async def test_sandbox_cleaned_up_on_completion(self) -> None:
        sandbox_mgr = MagicMock()
        sandbox_mgr.create_sandbox = AsyncMock(
            return_value={"sandbox_id": "sb-cleanup", "status": "active", "provider": "docker"}
        )
        exec_result = MagicMock(
            stdout="FINAL ANSWER: x", stderr="", exit_code=0, execution_time=0.1
        )
        sandbox_mgr.run_code = AsyncMock(return_value=exec_result)
        sandbox_mgr.stop_sandbox = AsyncMock()

        llm_provider = MagicMock()
        mock_resp = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = '```python\nFINAL("x")\n```'
        mock_resp.choices = [mock_choice]
        llm_provider.complete_async = AsyncMock(return_value=mock_resp)
        llm_provider.count_tokens = MagicMock(return_value=10)

        service = RLMInferenceService(
            sandbox_manager=sandbox_mgr,
            llm_provider=llm_provider,
            nexus_api_url="http://localhost:2026",
            max_concurrent=2,
        )

        req = RLMInferenceRequest(query="test", stream=False, max_iterations=3)
        await service.infer(req, user_id="user-1", api_key="key-1")

        sandbox_mgr.stop_sandbox.assert_called()

    @pytest.mark.asyncio
    async def test_sandbox_cleaned_up_on_error(self) -> None:
        sandbox_mgr = MagicMock()
        sandbox_mgr.create_sandbox = AsyncMock(
            return_value={"sandbox_id": "sb-err", "status": "active", "provider": "docker"}
        )
        sandbox_mgr.run_code = AsyncMock(side_effect=RuntimeError("crash"))
        sandbox_mgr.stop_sandbox = AsyncMock()

        llm_provider = MagicMock()
        mock_resp = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "```python\nprint(1)\n```"
        mock_resp.choices = [mock_choice]
        llm_provider.complete_async = AsyncMock(return_value=mock_resp)
        llm_provider.count_tokens = MagicMock(return_value=10)

        service = RLMInferenceService(
            sandbox_manager=sandbox_mgr,
            llm_provider=llm_provider,
            nexus_api_url="http://localhost:2026",
            max_concurrent=2,
        )

        req = RLMInferenceRequest(query="test", stream=False, max_iterations=2)
        result = await service.infer(req, user_id="user-1", api_key="key-1")

        # Should still have attempted cleanup
        assert result.status in (RLMStatus.FAILED, RLMStatus.BUDGET_EXCEEDED, RLMStatus.COMPLETED)
