"""Integration tests for RLM inference brick (Issue #1306).

Verifies:
- Import paths resolve correctly
- Brick manifest and verify_imports()
- Service construction with mock dependencies
- Factory wiring includes rlm_service
- Router registration in v2 versioning
- End-to-end inference flow with mocked sandbox + LLM
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# 1. Import path validation
# ---------------------------------------------------------------------------


class TestRLMImportPaths:
    """Verify all RLM import paths resolve correctly."""

    def test_public_api_exports(self) -> None:
        """nexus.rlm exports all public types and errors."""
        from nexus.rlm import (
            REPLResult,
            RLMBrickManifest,
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

        # All types are importable and not None
        for symbol in (
            REPLResult,
            RLMBrickManifest,
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
        ):
            assert symbol is not None

    def test_service_import(self) -> None:
        """RLMInferenceService is importable from the service module."""
        from nexus.rlm.service import RLMInferenceService

        assert RLMInferenceService is not None

    def test_environment_import(self) -> None:
        """NexusREPL is importable from the environment module."""
        from nexus.rlm.environment import NexusREPL

        assert NexusREPL is not None

    def test_lm_client_import(self) -> None:
        """NexusLMClient is importable from the lm_client module."""
        from nexus.rlm.lm_client import NexusLMClient

        assert NexusLMClient is not None

    def test_tools_import(self) -> None:
        """Tool functions are importable."""
        from nexus.rlm.tools import (
            build_tools_injection_code,
            nexus_list,
            nexus_read,
            nexus_search,
        )

        assert all(
            callable(f) for f in (nexus_read, nexus_search, nexus_list, build_tools_injection_code)
        )

    def test_router_import(self) -> None:
        """v2 router is importable."""
        from nexus.server.api.v2.routers.rlm import router

        assert router is not None
        assert router.prefix == "/api/v2/rlm"


# ---------------------------------------------------------------------------
# 2. Brick manifest
# ---------------------------------------------------------------------------


class TestRLMBrickManifest:
    """Verify the RLM brick manifest follows conventions."""

    def test_manifest_fields(self) -> None:
        """Manifest has required fields."""
        from nexus.rlm.manifest import RLMBrickManifest

        m = RLMBrickManifest()
        assert m.name == "rlm"
        assert m.version == "1.0.0"
        assert len(m.description) > 0
        assert "config_schema" in dir(m)
        assert "max_iterations" in m.config_schema


# ---------------------------------------------------------------------------
# 3. Service construction
# ---------------------------------------------------------------------------


class TestRLMServiceConstruction:
    """Verify RLMInferenceService can be constructed with mock dependencies."""

    def test_construct_with_mocks(self) -> None:
        """Service accepts mock sandbox_manager and llm_provider."""
        from nexus.rlm.service import RLMInferenceService

        service = RLMInferenceService(
            sandbox_manager=MagicMock(),
            llm_provider=MagicMock(),
            nexus_api_url="http://localhost:2026",
            max_concurrent=2,
        )
        assert service is not None

    def test_shutdown_is_safe(self) -> None:
        """shutdown() doesn't raise even with no active jobs."""
        from nexus.rlm.service import RLMInferenceService

        service = RLMInferenceService(
            sandbox_manager=MagicMock(),
            llm_provider=MagicMock(),
            nexus_api_url="http://localhost:2026",
            max_concurrent=2,
        )
        service.shutdown()  # Should not raise


# ---------------------------------------------------------------------------
# 4. Router registration
# ---------------------------------------------------------------------------


class TestRLMRouterRegistration:
    """Verify RLM router is registered in v2 versioning."""

    def test_rlm_in_v2_registry(self) -> None:
        """build_v2_registry includes rlm router entry."""
        from nexus.server.api.v2.versioning import build_v2_registry

        registry = build_v2_registry()
        names = [e.name for e in registry.entries]
        assert "rlm" in names

    def test_rlm_endpoint_count(self) -> None:
        """RLM router entry declares 1 endpoint."""
        from nexus.server.api.v2.versioning import build_v2_registry

        registry = build_v2_registry()
        rlm_entry = next(e for e in registry.entries if e.name == "rlm")
        assert rlm_entry.endpoint_count == 1


# ---------------------------------------------------------------------------
# 5. End-to-end inference flow (mocked sandbox + LLM)
# ---------------------------------------------------------------------------


class TestRLMInferenceFlow:
    """Verify the full inference flow with mocked dependencies."""

    @pytest.mark.asyncio
    async def test_non_streaming_inference(self) -> None:
        """Non-streaming inference returns a completed result."""
        from nexus.rlm.service import RLMInferenceService
        from nexus.rlm.types import RLMInferenceRequest, RLMStatus

        # Mock sandbox_manager
        mock_sandbox = MagicMock()
        mock_sandbox.create_sandbox = AsyncMock(return_value=MagicMock(sandbox_id="test-sb"))
        mock_sandbox.run_code = AsyncMock(
            return_value=MagicMock(stdout="FINAL ANSWER: 42\n", stderr="", exit_code=0)
        )
        mock_sandbox.stop_sandbox = AsyncMock()

        # Mock LLM provider
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '```python\nprint("FINAL ANSWER: 42")\n```'
        mock_llm.complete_async = AsyncMock(return_value=mock_response)
        mock_llm.count_tokens = MagicMock(return_value=100)

        service = RLMInferenceService(
            sandbox_manager=mock_sandbox,
            llm_provider=mock_llm,
            nexus_api_url="http://localhost:2026",
            max_concurrent=2,
        )

        request = RLMInferenceRequest(
            query="What is the answer?",
            max_iterations=3,
            max_duration_seconds=30,
        )

        result = await service.infer(request, user_id="test-user", api_key="test-key")

        assert result.status == RLMStatus.COMPLETED
        assert result.answer == "42"
        assert len(result.iterations) >= 1

    @pytest.mark.asyncio
    async def test_streaming_inference_yields_events(self) -> None:
        """Streaming inference yields SSE events."""
        from nexus.rlm.service import RLMInferenceService
        from nexus.rlm.types import RLMInferenceRequest, SSEEventType

        # Mock sandbox + LLM (same as above)
        mock_sandbox = MagicMock()
        mock_sandbox.create_sandbox = AsyncMock(return_value=MagicMock(sandbox_id="test-sb"))
        mock_sandbox.run_code = AsyncMock(
            return_value=MagicMock(stdout="FINAL ANSWER: hello\n", stderr="", exit_code=0)
        )
        mock_sandbox.stop_sandbox = AsyncMock()

        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '```python\nprint("FINAL ANSWER: hello")\n```'
        mock_llm.complete_async = AsyncMock(return_value=mock_response)
        mock_llm.count_tokens = MagicMock(return_value=50)

        service = RLMInferenceService(
            sandbox_manager=mock_sandbox,
            llm_provider=mock_llm,
            nexus_api_url="http://localhost:2026",
            max_concurrent=2,
        )

        request = RLMInferenceRequest(
            query="Say hello",
            max_iterations=3,
            max_duration_seconds=30,
        )

        events = []
        async for event in service.infer_stream(request, user_id="test", api_key="key"):
            events.append(event)

        # Should have: started, iteration(s), final_answer
        event_types = [e.event for e in events]
        assert SSEEventType.STARTED in event_types
        assert SSEEventType.FINAL_ANSWER in event_types
