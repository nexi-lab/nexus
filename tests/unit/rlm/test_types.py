"""Tests for RLM types and error hierarchy."""

from __future__ import annotations

import pytest

from nexus.rlm.types import (
    REPLResult,
    RLMBudgetExceededError,
    RLMCodeError,
    RLMError,
    RLMInferenceRequest,
    RLMInferenceResult,
    RLMInfrastructureError,
    RLMIteration,
    RLMStatus,
    SSEEvent,
    SSEEventType,
)


class TestErrorHierarchy:
    """All RLM errors must inherit from RLMError."""

    def test_infrastructure_error_is_rlm_error(self) -> None:
        err = RLMInfrastructureError("sandbox unavailable")
        assert isinstance(err, RLMError)
        assert str(err) == "sandbox unavailable"

    def test_code_error_is_rlm_error(self) -> None:
        err = RLMCodeError("syntax error in model code")
        assert isinstance(err, RLMError)

    def test_budget_exceeded_carries_partial_result(self) -> None:
        err = RLMBudgetExceededError(
            "max iterations reached",
            partial_result="partial answer",
            reason="iterations",
            iterations_used=15,
        )
        assert isinstance(err, RLMError)
        assert err.partial_result == "partial answer"
        assert err.reason == "iterations"
        assert err.iterations_used == 15

    def test_budget_exceeded_defaults(self) -> None:
        err = RLMBudgetExceededError("limit hit")
        assert err.partial_result is None
        assert err.reason == "unknown"
        assert err.iterations_used == 0


class TestRLMInferenceRequest:
    """Request validation and defaults."""

    def test_defaults(self) -> None:
        req = RLMInferenceRequest(query="What is X?")
        assert req.query == "What is X?"
        assert req.context_paths == ()
        assert req.zone_id == "root"
        assert req.model == "claude-sonnet-4-20250514"
        assert req.sub_model is None
        assert req.max_iterations == 15
        assert req.max_duration_seconds == 120
        assert req.max_total_tokens == 100_000
        assert req.sandbox_provider is None
        assert req.stream is True

    def test_with_context_paths(self) -> None:
        req = RLMInferenceRequest(
            query="Summarize",
            context_paths=("/workspace/doc1.md", "/workspace/doc2.md"),
        )
        assert len(req.context_paths) == 2

    def test_max_iterations_too_low_raises(self) -> None:
        with pytest.raises(ValueError, match="max_iterations must be 1-50"):
            RLMInferenceRequest(query="test", max_iterations=0)

    def test_max_iterations_too_high_raises(self) -> None:
        with pytest.raises(ValueError, match="max_iterations must be 1-50"):
            RLMInferenceRequest(query="test", max_iterations=51)

    def test_max_duration_too_low_raises(self) -> None:
        with pytest.raises(ValueError, match="max_duration_seconds must be 10-600"):
            RLMInferenceRequest(query="test", max_duration_seconds=5)

    def test_max_duration_too_high_raises(self) -> None:
        with pytest.raises(ValueError, match="max_duration_seconds must be 10-600"):
            RLMInferenceRequest(query="test", max_duration_seconds=601)

    def test_max_tokens_too_low_raises(self) -> None:
        with pytest.raises(ValueError, match="max_total_tokens must be 1K-1M"):
            RLMInferenceRequest(query="test", max_total_tokens=500)

    def test_max_tokens_too_high_raises(self) -> None:
        with pytest.raises(ValueError, match="max_total_tokens must be 1K-1M"):
            RLMInferenceRequest(query="test", max_total_tokens=2_000_000)

    def test_immutable(self) -> None:
        req = RLMInferenceRequest(query="test")
        with pytest.raises(AttributeError):
            req.query = "changed"  # type: ignore[misc]


class TestREPLResult:
    """REPL execution result."""

    def test_defaults(self) -> None:
        result = REPLResult()
        assert result.stdout == ""
        assert result.stderr == ""
        assert result.execution_time == 0.0
        assert result.exit_code == 0

    def test_with_output(self) -> None:
        result = REPLResult(stdout="hello\n", stderr="", execution_time=0.5, exit_code=0)
        assert result.stdout == "hello\n"
        assert result.execution_time == 0.5

    def test_immutable(self) -> None:
        result = REPLResult()
        with pytest.raises(AttributeError):
            result.stdout = "changed"  # type: ignore[misc]


class TestRLMIteration:
    """Single iteration record."""

    def test_creation(self) -> None:
        repl = REPLResult(stdout="42\n")
        iteration = RLMIteration(
            step=1,
            code_executed="print(42)",
            repl_result=repl,
            tokens_used=150,
            duration_seconds=2.3,
        )
        assert iteration.step == 1
        assert iteration.code_executed == "print(42)"
        assert iteration.repl_result.stdout == "42\n"
        assert iteration.tokens_used == 150


class TestRLMInferenceResult:
    """Final inference result."""

    def test_completed_result(self) -> None:
        result = RLMInferenceResult(
            status=RLMStatus.COMPLETED,
            answer="The answer is 42",
            total_tokens=5000,
            total_duration_seconds=30.5,
        )
        assert result.status == RLMStatus.COMPLETED
        assert result.answer == "The answer is 42"

    def test_failed_result(self) -> None:
        result = RLMInferenceResult(
            status=RLMStatus.FAILED,
            error_message="Sandbox creation failed",
        )
        assert result.status == RLMStatus.FAILED
        assert result.answer is None
        assert result.error_message == "Sandbox creation failed"


class TestSSEEvent:
    """Server-Sent Event types."""

    def test_event_types(self) -> None:
        assert SSEEventType.STARTED == "rlm.started"
        assert SSEEventType.ITERATION == "rlm.iteration"
        assert SSEEventType.FINAL_ANSWER == "rlm.final_answer"
        assert SSEEventType.ERROR == "rlm.error"
        assert SSEEventType.BUDGET_EXCEEDED == "rlm.budget_exceeded"

    def test_sse_event_creation(self) -> None:
        event = SSEEvent(
            event=SSEEventType.ITERATION,
            data={"step": 3, "total_steps": 15},
        )
        assert event.event == SSEEventType.ITERATION
        assert event.data["step"] == 3


class TestRLMStatus:
    """Status enum values."""

    def test_all_statuses(self) -> None:
        assert RLMStatus.PENDING == "pending"
        assert RLMStatus.RUNNING == "running"
        assert RLMStatus.COMPLETED == "completed"
        assert RLMStatus.FAILED == "failed"
        assert RLMStatus.BUDGET_EXCEEDED == "budget_exceeded"
        assert RLMStatus.CANCELLED == "cancelled"
