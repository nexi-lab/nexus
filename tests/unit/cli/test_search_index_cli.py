from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from nexus.cli.commands import search as search_module
from nexus.cli.commands.search import search_index


@pytest.fixture(autouse=True)
def _disable_auto_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_NO_AUTO_JSON", "1")


def test_search_index_accepts_daemon_stats_total_files(monkeypatch: pytest.MonkeyPatch) -> None:
    search_svc = MagicMock()
    search_svc.semantic_search_index.return_value = {
        "indexed": {"/workspace/demo/README.md": 3},
        "total_chunks": 3,
    }
    search_svc.semantic_search_stats.return_value = {
        "backend": "PgFtsBackend",
        "total_files": 1,
        "total_chunks": 3,
    }

    nx = MagicMock()
    nx.service.return_value = search_svc

    async def _get_filesystem(remote_url: str | None, remote_api_key: str | None):
        return nx

    monkeypatch.setattr(search_module, "get_filesystem", _get_filesystem)

    result = CliRunner().invoke(
        search_index,
        ["/workspace/demo", "--remote-url", "http://127.0.0.1:2026", "--remote-api-key", "k"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Files indexed:" in result.output
    assert "Total indexed files:" in result.output
    nx.close.assert_called_once()
