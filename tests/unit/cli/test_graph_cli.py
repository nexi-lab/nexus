"""Tests for nexus graph CLI commands."""

from __future__ import annotations

import json
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from nexus.cli.clients.graph import GraphClient
from nexus.cli.commands.graph_cli import graph

MOCK_URL = "http://localhost:2026"
_ENV = {"NEXUS_NO_AUTO_JSON": "1"}


@contextmanager
def _mock_client(**overrides: Any):
    """Patch GraphClient so service_command uses a mock instance.

    The @service_command decorator captures client_class in a closure at import
    time, so patching the module-level name has no effect.  Instead we patch the
    class methods directly via ``patch.object``.
    """
    with ExitStack() as stack:
        stack.enter_context(patch.object(GraphClient, "__init__", lambda self, **kw: None))
        stack.enter_context(patch.object(GraphClient, "__enter__", lambda self: self))
        stack.enter_context(patch.object(GraphClient, "__exit__", lambda self, *a: False))
        mocks: dict[str, MagicMock] = {}
        for name, retval in overrides.items():
            m = MagicMock(return_value=retval)
            stack.enter_context(patch.object(GraphClient, name, m))
            mocks[name] = m
        yield mocks


class TestGraphEntity:
    def test_happy_path(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(entity={"entity": {"entity_id": "e1", "type": "concept", "label": "ML"}}):
            result = runner.invoke(graph, ["entity", "e1", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "ML" in result.output

    def test_json_output(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(entity={"entity": {"entity_id": "e1", "type": "concept", "label": "ML"}}):
            result = runner.invoke(graph, ["entity", "e1", "--remote-url", MOCK_URL, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["entity"]["entity_id"] == "e1"

    def test_with_properties(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(
            entity={
                "entity": {
                    "entity_id": "e1",
                    "type": "concept",
                    "label": "ML",
                    "properties": {"domain": "AI", "year": 2020},
                }
            }
        ):
            result = runner.invoke(graph, ["entity", "e1", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "domain" in result.output

    def test_client_called_with_entity_id(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(entity={"entity": {"entity_id": "e1"}}) as mocks:
            runner.invoke(graph, ["entity", "e1", "--remote-url", MOCK_URL])
        mocks["entity"].assert_called_once_with("e1")

    def test_missing_url_fails(self) -> None:
        runner = CliRunner(env=_ENV)
        result = runner.invoke(graph, ["entity", "e1"])
        assert result.exit_code != 0


class TestGraphNeighbors:
    def test_happy_path(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(
            neighbors={
                "neighbors": [
                    {
                        "entity": {
                            "entity_id": "e2",
                            "type": "concept",
                            "label": "DL",
                        },
                        "depth": 1,
                        "path": [],
                    }
                ]
            }
        ):
            result = runner.invoke(graph, ["neighbors", "e1", "--remote-url", MOCK_URL])
        assert result.exit_code == 0

    def test_default_hops_is_1(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(neighbors={"neighbors": []}) as mocks:
            runner.invoke(graph, ["neighbors", "e1", "--remote-url", MOCK_URL])
        mocks["neighbors"].assert_called_once_with("e1", hops=1)

    def test_with_hops(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(neighbors={"neighbors": []}) as mocks:
            result = runner.invoke(
                graph, ["neighbors", "e1", "--hops", "3", "--remote-url", MOCK_URL]
            )
        assert result.exit_code == 0
        mocks["neighbors"].assert_called_once_with("e1", hops=3)

    def test_empty_neighbors(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(neighbors={"neighbors": []}):
            result = runner.invoke(graph, ["neighbors", "e1", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "No neighbors" in result.output

    def test_json_output(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(
            neighbors={
                "neighbors": [
                    {
                        "entity": {"entity_id": "e2", "type": "concept", "label": "DL"},
                        "depth": 1,
                        "path": [],
                    }
                ]
            }
        ):
            result = runner.invoke(graph, ["neighbors", "e1", "--remote-url", MOCK_URL, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["data"]["neighbors"]) == 1


class TestGraphSubgraph:
    def test_happy_path(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(
            subgraph={
                "nodes": [{"entity_id": "e1"}, {"entity_id": "e2"}],
                "edges": [{"source": "e1", "target": "e2"}],
            }
        ):
            result = runner.invoke(graph, ["subgraph", "e1", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "Nodes: 2" in result.output
        assert "Edges: 1" in result.output

    def test_default_max_hops_is_2(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(subgraph={"nodes": [], "edges": []}) as mocks:
            runner.invoke(graph, ["subgraph", "e1", "--remote-url", MOCK_URL])
        mocks["subgraph"].assert_called_once_with(["e1"], max_hops=2)

    def test_with_max_hops(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(subgraph={"nodes": [], "edges": []}) as mocks:
            runner.invoke(graph, ["subgraph", "e1", "--max-hops", "3", "--remote-url", MOCK_URL])
        mocks["subgraph"].assert_called_once_with(["e1"], max_hops=3)

    def test_json_output(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(subgraph={"nodes": [{"entity_id": "e1"}], "edges": []}):
            result = runner.invoke(graph, ["subgraph", "e1", "--remote-url", MOCK_URL, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["data"]["nodes"]) == 1


class TestGraphSearch:
    def test_happy_path(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(
            search={
                "results": [
                    {
                        "entity_id": "e1",
                        "type": "concept",
                        "label": "ML",
                        "score": 0.95,
                    }
                ]
            }
        ):
            result = runner.invoke(graph, ["search", "machine learning", "--remote-url", MOCK_URL])
        assert result.exit_code == 0

    def test_empty_results(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(search={"results": []}):
            result = runner.invoke(graph, ["search", "nonexistent", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "No matching entities" in result.output

    def test_json_output(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(
            search={
                "results": [{"entity_id": "e1", "type": "concept", "label": "ML", "score": 0.95}]
            }
        ):
            result = runner.invoke(graph, ["search", "ML", "--remote-url", MOCK_URL, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["data"]["results"]) == 1

    def test_client_called_with_name(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(search={"results": []}) as mocks:
            runner.invoke(graph, ["search", "agent collaboration", "--remote-url", MOCK_URL])
        mocks["search"].assert_called_once_with(
            "agent collaboration", entity_type=None, fuzzy=False
        )
