"""Tests for LLMSubsystem — Phase B extraction.

Issue #1287: Extract NexusFS Domain Services from God Object.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.services.subsystem import Subsystem
from nexus.services.subsystems.llm_subsystem import LLMSubsystem


@pytest.fixture
def mock_llm_service() -> MagicMock:
    """Create a mock LLMService."""
    svc = MagicMock()
    svc.llm_read = AsyncMock(return_value="test answer")
    svc.llm_read_detailed = AsyncMock(
        return_value=MagicMock(answer="detailed answer", citations=[], cost=0.01)
    )
    svc.llm_read_stream = AsyncMock()
    svc.create_llm_reader = MagicMock(return_value=MagicMock())
    return svc


@pytest.fixture
def subsystem(mock_llm_service: MagicMock) -> LLMSubsystem:
    """Create LLMSubsystem for tests."""
    return LLMSubsystem(llm_service=mock_llm_service)


# ---------------------------------------------------------------------------
# Subsystem ABC compliance (same 3 tests for every subsystem)
# ---------------------------------------------------------------------------


class TestLLMSubsystemCompliance:
    """Subsystem ABC contract tests for LLMSubsystem."""

    def test_health_check_returns_dict_with_status(self, subsystem: LLMSubsystem) -> None:
        result = subsystem.health_check()
        assert isinstance(result, dict)
        assert "status" in result
        assert result["status"] in ("ok", "degraded")

    def test_cleanup_callable_and_no_raise(self, subsystem: LLMSubsystem) -> None:
        assert callable(subsystem.cleanup)
        subsystem.cleanup()

    def test_is_subsystem_instance(self, subsystem: LLMSubsystem) -> None:
        assert isinstance(subsystem, Subsystem)


# ---------------------------------------------------------------------------
# LLM-specific tests
# ---------------------------------------------------------------------------


class TestLLMSubsystem:
    """LLMSubsystem-specific tests."""

    def test_service_property(self, subsystem: LLMSubsystem, mock_llm_service: MagicMock) -> None:
        """Service property returns the underlying LLMService."""
        assert subsystem.service is mock_llm_service

    def test_health_check_reports_ok(self, subsystem: LLMSubsystem) -> None:
        """Health check returns ok status with subsystem name."""
        health = subsystem.health_check()
        assert health["status"] == "ok"
        assert health["subsystem"] == "llm"
        assert health["service_configured"] is True

    @pytest.mark.asyncio
    async def test_delegates_llm_read(
        self, subsystem: LLMSubsystem, mock_llm_service: MagicMock
    ) -> None:
        """Subsystem delegates llm_read to service."""
        result = await subsystem.service.llm_read(
            path="/doc.txt",
            prompt="What is this?",
        )
        assert result == "test answer"
        mock_llm_service.llm_read.assert_awaited_once_with(
            path="/doc.txt",
            prompt="What is this?",
        )

    @pytest.mark.asyncio
    async def test_delegates_llm_read_detailed(
        self, subsystem: LLMSubsystem, mock_llm_service: MagicMock
    ) -> None:
        """Subsystem delegates llm_read_detailed to service."""
        result = await subsystem.service.llm_read_detailed(
            path="/doc.txt",
            prompt="Summarize",
            include_citations=True,
        )
        assert result.answer == "detailed answer"

    def test_delegates_create_llm_reader(
        self, subsystem: LLMSubsystem, mock_llm_service: MagicMock
    ) -> None:
        """Subsystem delegates create_llm_reader to service."""
        reader = subsystem.service.create_llm_reader(model="test-model")
        assert reader is not None
        mock_llm_service.create_llm_reader.assert_called_once_with(model="test-model")
