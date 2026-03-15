"""Tests for _safe_json_loads helper in agent_registry (Issue #1427, #2984).

The context_manifest column and its round-trip tests have been removed
(Issue #2984: context assembly moved to stateless MCP tool). These tests
cover the _safe_json_loads helper that remains in use for agent_metadata.
"""

from nexus.system_services.agents.agent_registry import _safe_json_loads


class TestSafeJsonLoads:
    def test_none_returns_default_dict_for_metadata(self) -> None:
        assert _safe_json_loads(None, "agent_metadata", "a1") == {}

    def test_none_returns_default_list_for_other(self) -> None:
        assert _safe_json_loads(None, "some_field", "a1") == []

    def test_empty_string_returns_default(self) -> None:
        assert _safe_json_loads("", "agent_metadata", "a1") == {}
        assert _safe_json_loads("", "some_field", "a1") == []

    def test_corrupt_json_returns_default(self) -> None:
        assert _safe_json_loads("{invalid", "agent_metadata", "a1") == {}
        assert _safe_json_loads("[broken", "some_field", "a1") == []

    def test_valid_json_dict(self) -> None:
        assert _safe_json_loads('{"key": "val"}', "agent_metadata", "a1") == {"key": "val"}

    def test_valid_json_list(self) -> None:
        assert _safe_json_loads('[{"a": 1}]', "some_field", "a1") == [{"a": 1}]
