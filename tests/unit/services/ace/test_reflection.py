"""Unit tests for Reflector.

Tests prompt building, response parsing, fallback reflection logic,
and the reflect_async orchestration with mocked LLM and trajectory dependencies.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.services.ace.reflection import Reflector


def _make_mock_backend() -> MagicMock:
    """Create a mock CAS backend."""
    backend = MagicMock()
    call_count = [0]

    def write_content(data: bytes) -> MagicMock:
        call_count[0] += 1
        result = MagicMock()
        result.unwrap.return_value = f"hash-{call_count[0]}"
        return result

    backend.write_content = write_content
    return backend


def _make_mock_session() -> MagicMock:
    session = MagicMock()
    session.add = MagicMock()
    session.commit = MagicMock()
    return session


def _make_trajectory_data(
    status: str = "success",
    success_score: float = 0.9,
    steps: list[dict[str, Any]] | None = None,
    decisions: list[dict[str, Any]] | None = None,
    observations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create fake trajectory data for testing."""
    return {
        "trajectory_id": "traj-1",
        "user_id": "user-1",
        "agent_id": "agent-1",
        "task_description": "Analyze data quality",
        "task_type": "data_processing",
        "status": status,
        "success_score": success_score,
        "error_message": None,
        "duration_ms": 1500,
        "tokens_used": 200,
        "cost_usd": 0.01,
        "started_at": "2025-01-01T00:00:00",
        "completed_at": "2025-01-01T00:00:01",
        "trace": {
            "steps": steps or [{"description": "Step 1", "result": "ok"}],
            "decisions": decisions or [],
            "observations": observations or [],
        },
    }


# ---------------------------------------------------------------------------
# _build_reflection_prompt
# ---------------------------------------------------------------------------


class TestBuildReflectionPrompt:
    """Tests for _build_reflection_prompt."""

    def test_includes_task_description(self) -> None:
        session = _make_mock_session()
        backend = _make_mock_backend()
        trajectory_mgr = MagicMock()
        llm = MagicMock()
        reflector = Reflector(session, backend, llm, trajectory_mgr, user_id="user-1")

        data = _make_trajectory_data()
        prompt = reflector._build_reflection_prompt(data, None)
        assert "Analyze data quality" in prompt

    def test_includes_status(self) -> None:
        session = _make_mock_session()
        backend = _make_mock_backend()
        trajectory_mgr = MagicMock()
        llm = MagicMock()
        reflector = Reflector(session, backend, llm, trajectory_mgr, user_id="user-1")

        data = _make_trajectory_data(status="failure")
        prompt = reflector._build_reflection_prompt(data, None)
        assert "failure" in prompt

    def test_includes_context(self) -> None:
        session = _make_mock_session()
        backend = _make_mock_backend()
        trajectory_mgr = MagicMock()
        llm = MagicMock()
        reflector = Reflector(session, backend, llm, trajectory_mgr, user_id="user-1")

        data = _make_trajectory_data()
        prompt = reflector._build_reflection_prompt(data, "Extra context here")
        assert "Extra context here" in prompt

    def test_includes_steps(self) -> None:
        session = _make_mock_session()
        backend = _make_mock_backend()
        trajectory_mgr = MagicMock()
        llm = MagicMock()
        reflector = Reflector(session, backend, llm, trajectory_mgr, user_id="user-1")

        data = _make_trajectory_data(
            steps=[
                {"description": "Called API", "result": "200 OK"},
            ]
        )
        prompt = reflector._build_reflection_prompt(data, None)
        assert "Called API" in prompt


# ---------------------------------------------------------------------------
# _format_trace_items
# ---------------------------------------------------------------------------


class TestFormatTraceItems:
    """Tests for _format_trace_items."""

    def test_empty_items(self) -> None:
        session = _make_mock_session()
        backend = _make_mock_backend()
        reflector = Reflector(session, backend, MagicMock(), MagicMock(), user_id="u1")
        result = reflector._format_trace_items([])
        assert result == "(none)"

    def test_items_with_result(self) -> None:
        session = _make_mock_session()
        backend = _make_mock_backend()
        reflector = Reflector(session, backend, MagicMock(), MagicMock(), user_id="u1")
        items = [{"description": "Step 1", "result": "Success"}]
        result = reflector._format_trace_items(items)
        assert "Step 1" in result
        assert "Success" in result

    def test_items_without_result(self) -> None:
        session = _make_mock_session()
        backend = _make_mock_backend()
        reflector = Reflector(session, backend, MagicMock(), MagicMock(), user_id="u1")
        items = [{"description": "Step 1", "result": ""}]
        result = reflector._format_trace_items(items)
        assert "Step 1" in result


# ---------------------------------------------------------------------------
# _parse_reflection_response
# ---------------------------------------------------------------------------


class TestParseReflectionResponse:
    """Tests for _parse_reflection_response."""

    def _make_reflector(self) -> Reflector:
        return Reflector(
            _make_mock_session(),
            _make_mock_backend(),
            MagicMock(),
            MagicMock(),
            user_id="u1",
        )

    def test_valid_json_response(self) -> None:
        reflector = self._make_reflector()
        data = _make_trajectory_data()
        json_response = json.dumps(
            {
                "helpful_strategies": [{"description": "Good strategy", "confidence": 0.9}],
                "harmful_patterns": [],
                "observations": [],
                "confidence": 0.85,
            }
        )
        result = reflector._parse_reflection_response(json_response, data)
        assert len(result["helpful_strategies"]) == 1
        assert result["confidence"] == 0.85

    def test_json_in_code_block(self) -> None:
        reflector = self._make_reflector()
        data = _make_trajectory_data()
        response = '```json\n{"helpful_strategies": [], "confidence": 0.7}\n```'
        result = reflector._parse_reflection_response(response, data)
        assert result["confidence"] == 0.7

    def test_json_in_generic_code_block(self) -> None:
        reflector = self._make_reflector()
        data = _make_trajectory_data()
        response = '```\n{"helpful_strategies": [], "confidence": 0.6}\n```'
        result = reflector._parse_reflection_response(response, data)
        assert result["confidence"] == 0.6

    def test_invalid_json_falls_back(self) -> None:
        reflector = self._make_reflector()
        data = _make_trajectory_data()
        result = reflector._parse_reflection_response("not valid json at all", data)
        # Fallback should still return a valid structure
        assert "helpful_strategies" in result
        assert "harmful_patterns" in result
        assert "confidence" in result

    def test_none_response_falls_back(self) -> None:
        reflector = self._make_reflector()
        data = _make_trajectory_data()
        result = reflector._parse_reflection_response(None, data)
        assert "helpful_strategies" in result
        assert "confidence" in result


# ---------------------------------------------------------------------------
# _create_fallback_reflection
# ---------------------------------------------------------------------------


class TestCreateFallbackReflection:
    """Tests for _create_fallback_reflection."""

    def test_successful_trajectory(self) -> None:
        reflector = Reflector(
            _make_mock_session(),
            _make_mock_backend(),
            MagicMock(),
            MagicMock(),
            user_id="u1",
        )
        data = _make_trajectory_data(status="success", success_score=0.9)
        result = reflector._create_fallback_reflection(data)
        assert len(result["helpful_strategies"]) >= 1
        assert result["confidence"] == 0.4

    def test_failed_trajectory(self) -> None:
        reflector = Reflector(
            _make_mock_session(),
            _make_mock_backend(),
            MagicMock(),
            MagicMock(),
            user_id="u1",
        )
        data = _make_trajectory_data(status="failure", success_score=0.1)
        data["error_message"] = "Connection timeout"
        result = reflector._create_fallback_reflection(data)
        assert len(result["harmful_patterns"]) >= 1

    def test_observations_with_error_types(self) -> None:
        reflector = Reflector(
            _make_mock_session(),
            _make_mock_backend(),
            MagicMock(),
            MagicMock(),
            user_id="u1",
        )
        data = _make_trajectory_data(
            observations=[
                {
                    "description": "Validation check",
                    "result": {
                        "error_types": {"invalid_age": 5, "missing_name": 3},
                    },
                }
            ],
        )
        result = reflector._create_fallback_reflection(data)
        assert len(result["helpful_strategies"]) >= 1


# ---------------------------------------------------------------------------
# reflect_async orchestration
# ---------------------------------------------------------------------------


class TestReflectAsync:
    """Tests for reflect_async with mocked LLM."""

    @pytest.mark.asyncio
    async def test_reflect_async_happy_path(self) -> None:
        session = _make_mock_session()
        backend = _make_mock_backend()
        trajectory_mgr = MagicMock()
        trajectory_mgr.get_trajectory.return_value = _make_trajectory_data()

        llm_response = MagicMock()
        llm_response.content = json.dumps(
            {
                "helpful_strategies": [{"description": "Use caching", "confidence": 0.8}],
                "harmful_patterns": [],
                "observations": [],
                "confidence": 0.75,
            }
        )
        llm = AsyncMock()
        llm.complete_async.return_value = llm_response

        reflector = Reflector(session, backend, llm, trajectory_mgr, user_id="u1")
        result = await reflector.reflect_async("traj-1")

        assert result["trajectory_id"] == "traj-1"
        assert result["memory_id"] is not None
        assert len(result["helpful_strategies"]) == 1

    @pytest.mark.asyncio
    async def test_reflect_async_trajectory_not_found(self) -> None:
        session = _make_mock_session()
        backend = _make_mock_backend()
        trajectory_mgr = MagicMock()
        trajectory_mgr.get_trajectory.return_value = None

        reflector = Reflector(session, backend, MagicMock(), trajectory_mgr, user_id="u1")
        with pytest.raises(ValueError, match="not found"):
            await reflector.reflect_async("nonexistent")


# ---------------------------------------------------------------------------
# _store_reflection
# ---------------------------------------------------------------------------


class TestStoreReflection:
    """Tests for _store_reflection."""

    def test_stores_memory_and_returns_id(self) -> None:
        session = _make_mock_session()
        backend = _make_mock_backend()
        reflector = Reflector(session, backend, MagicMock(), MagicMock(), user_id="u1")

        reflection_data = {
            "helpful_strategies": [],
            "harmful_patterns": [],
            "confidence": 0.5,
        }
        memory_id = reflector._store_reflection("traj-1", reflection_data)
        assert isinstance(memory_id, str)
        assert len(memory_id) > 0
        session.add.assert_called_once()
        session.commit.assert_called_once()
