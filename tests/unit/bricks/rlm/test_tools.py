"""Tests for RLM REPL tools — pre-loaded functions for sandbox access.

Tests verify:
- nexus_read() calls Nexus REST API correctly
- nexus_search() calls search endpoint correctly
- nexus_list() calls list endpoint correctly
- Error handling for HTTP failures
- Output formatting
"""

from unittest.mock import MagicMock, patch

from nexus.bricks.rlm.tools import (
    build_tools_injection_code,
    nexus_list,
    nexus_read,
    nexus_search,
)
from nexus.contracts.constants import ROOT_ZONE_ID


class TestNexusRead:
    """nexus_read() fetches file content via Nexus REST API."""

    @patch("nexus.bricks.rlm.tools.requests.get")
    def test_read_returns_content(self, mock_get: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "File content here"
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = nexus_read(
            "/workspace/doc.md",
            api_url="http://localhost:2026",
            api_key="test-key",
            zone_id=ROOT_ZONE_ID,
        )

        assert result == "File content here"
        mock_get.assert_called_once()
        call_url = mock_get.call_args.args[0]
        assert "/api/v2/files/" in call_url

    @patch("nexus.bricks.rlm.tools.requests.get")
    def test_read_passes_auth_header(self, mock_get: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "content"
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        nexus_read(
            "/workspace/doc.md",
            api_url="http://localhost:2026",
            api_key="test-key",
            zone_id=ROOT_ZONE_ID,
        )

        call_kwargs = mock_get.call_args.kwargs
        assert "headers" in call_kwargs
        assert call_kwargs["headers"].get("Authorization") == "Bearer test-key"

    @patch("nexus.bricks.rlm.tools.requests.get")
    def test_read_404_returns_error_message(self, mock_get: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = "Not Found"
        mock_resp.raise_for_status = MagicMock(side_effect=Exception("404 Not Found"))
        mock_get.return_value = mock_resp

        result = nexus_read(
            "/nonexistent/file.md",
            api_url="http://localhost:2026",
            api_key="test-key",
            zone_id=ROOT_ZONE_ID,
        )

        assert "error" in result.lower() or "Error" in result

    @patch("nexus.bricks.rlm.tools.requests.get")
    def test_read_network_error(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = ConnectionError("Connection refused")

        result = nexus_read(
            "/workspace/doc.md",
            api_url="http://localhost:2026",
            api_key="test-key",
            zone_id=ROOT_ZONE_ID,
        )

        assert "error" in result.lower() or "Error" in result


class TestNexusSearch:
    """nexus_search() queries Nexus search endpoint."""

    @patch("nexus.bricks.rlm.tools.requests.get")
    def test_search_returns_results(self, mock_get: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [
                {"path": "/doc1.md", "content": "Result 1", "score": 0.95},
                {"path": "/doc2.md", "content": "Result 2", "score": 0.87},
            ]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = nexus_search(
            "quantum computing",
            api_url="http://localhost:2026",
            api_key="test-key",
            zone_id=ROOT_ZONE_ID,
        )

        assert isinstance(result, str)
        assert "doc1.md" in result or "Result 1" in result

    @patch("nexus.bricks.rlm.tools.requests.get")
    def test_search_with_limit(self, mock_get: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"results": []}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        nexus_search(
            "test query",
            api_url="http://localhost:2026",
            api_key="test-key",
            zone_id=ROOT_ZONE_ID,
            limit=5,
        )

        call_kwargs = mock_get.call_args.kwargs
        assert call_kwargs.get("params", {}).get("limit") == "5" or "limit=5" in str(
            mock_get.call_args
        )

    @patch("nexus.bricks.rlm.tools.requests.get")
    def test_search_error_returns_message(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = ConnectionError("timeout")

        result = nexus_search(
            "test",
            api_url="http://localhost:2026",
            api_key="test-key",
            zone_id=ROOT_ZONE_ID,
        )

        assert "error" in result.lower() or "Error" in result


class TestNexusList:
    """nexus_list() lists directory contents via Nexus API."""

    @patch("nexus.bricks.rlm.tools.requests.get")
    def test_list_returns_entries(self, mock_get: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "entries": [
                {"name": "doc1.md", "type": "file", "size": 1024},
                {"name": "subdir", "type": "directory"},
            ]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = nexus_list(
            "/workspace/",
            api_url="http://localhost:2026",
            api_key="test-key",
            zone_id=ROOT_ZONE_ID,
        )

        assert isinstance(result, str)
        assert "doc1.md" in result

    @patch("nexus.bricks.rlm.tools.requests.get")
    def test_list_error_returns_message(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = ConnectionError("timeout")

        result = nexus_list(
            "/workspace/",
            api_url="http://localhost:2026",
            api_key="test-key",
            zone_id=ROOT_ZONE_ID,
        )

        assert "error" in result.lower() or "Error" in result


class TestBuildToolsInjectionCode:
    """build_tools_injection_code() generates Python code for sandbox injection."""

    def test_generates_valid_python(self) -> None:
        code = build_tools_injection_code(
            api_url="http://localhost:2026",
            api_key="test-key",
            zone_id=ROOT_ZONE_ID,
        )

        assert isinstance(code, str)
        # Should be valid Python (compile check)
        compile(code, "<test>", "exec")

    def test_includes_nexus_read_function(self) -> None:
        code = build_tools_injection_code(
            api_url="http://localhost:2026",
            api_key="test-key",
            zone_id=ROOT_ZONE_ID,
        )
        assert "def nexus_read" in code

    def test_includes_nexus_search_function(self) -> None:
        code = build_tools_injection_code(
            api_url="http://localhost:2026",
            api_key="test-key",
            zone_id=ROOT_ZONE_ID,
        )
        assert "def nexus_search" in code

    def test_includes_final_function(self) -> None:
        code = build_tools_injection_code(
            api_url="http://localhost:2026",
            api_key="test-key",
            zone_id=ROOT_ZONE_ID,
        )
        assert "FINAL" in code

    def test_embeds_api_url(self) -> None:
        code = build_tools_injection_code(
            api_url="http://custom:9999",
            api_key="my-key",
            zone_id="my-zone",
        )
        assert "http://custom:9999" in code
