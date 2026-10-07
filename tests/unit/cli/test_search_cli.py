"""CLI HTTP contracts, connection profiles, counters, and failure exit codes."""

import json

import httpx
import pytest
from click.testing import CliRunner

from nexus.cli.commands.search import semantic_search_group
from nexus.cli.config import NexusCliConfig, ProfileEntry, save_cli_config

STATS = {
    "backend": "rust-plugin",
    "fts_path_count": 2,
    "fts_doc_count": 7,
    "ann_chunk_count": 0,
    "pending": 0,
    "embedding_model": "",
}


@pytest.fixture
def http_server(monkeypatch, tmp_path):
    from nexus.cli import config

    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.yaml")
    requests = []
    responses = {}
    original = httpx.Client

    def receive(request):
        requests.append(request)
        status, body = responses.get(request.url.path, (200, STATS))
        return httpx.Response(status, json=body)

    def client(*args, **kwargs):
        return original(*args, **kwargs, transport=httpx.MockTransport(receive))

    monkeypatch.setattr(httpx, "Client", client)
    return requests, responses


def invoke(*args, env=None, obj=None):
    return CliRunner().invoke(
        semantic_search_group,
        list(args),
        env={"NEXUS_URL": "", "NEXUS_API_KEY": "", "NEXUS_ZONE_ID": "", **(env or {})},
        obj=obj,
    )


def test_query_preserves_json_array_and_rust_request(http_server):
    requests, responses = http_server
    hits = [{"path": "/docs/a.txt", "score": 1.25, "chunk_text": "constellation"}]
    responses["/v2/search/query"] = (200, {"results": hits})
    result = invoke(
        "query",
        "constellation",
        "--path",
        "/docs",
        "--limit",
        "3",
        "--mode",
        "keyword",
        "--zone-id",
        "sharedzone",
        "--json",
        "--remote-url",
        "http://cluster:2027",
        "--remote-api-key",
        "sk-reader",
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == hits
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST" and request.url.path == "/v2/search/query"
    assert request.headers["authorization"] == "Bearer sk-reader"
    assert json.loads(request.content) == {
        "q": "constellation",
        "path_filter": "/docs",
        "limit": 3,
        "query_type": "keyword",
        "zone_id": "sharedzone",
    }


def test_index_reports_exact_counts_and_honors_nonrecursive(http_server):
    requests, responses = http_server
    responses["/v2/documents/index"] = (200, {"indexed_count": 2, "skipped_count": 1})
    result = invoke(
        "index",
        "/docs",
        "--no-recursive",
        "--zone-id",
        "sharedzone",
        "--remote-url",
        "http://cluster:2027",
    )
    assert result.exit_code == 0, result.output
    assert "Files indexed: 2" in result.output and "Files skipped: 1" in result.output
    assert "Indexed files: 2" in result.output
    assert "Keyword chunks: 7" in result.output and "Vector chunks: 0" in result.output
    assert [r.url.path for r in requests] == ["/v2/documents/index", "/v2/documents/stats"]
    assert json.loads(requests[0].content) == {
        "root_path": "/docs",
        "recursive": False,
        "zone_id": "sharedzone",
    }
    assert requests[1].url.params["zone_id"] == "sharedzone"


@pytest.mark.parametrize("named", [False, True])
def test_profile_supplies_url_key_and_zone(http_server, named):
    requests, _ = http_server
    save_cli_config(
        NexusCliConfig(
            current_profile=None if named else "production",
            profiles={
                "production": ProfileEntry("http://cluster:2027", "sk-profile", "sharedzone")
            },
        )
    )
    result = invoke("stats", obj={"profile": "production"} if named else None)
    assert result.exit_code == 0, result.output
    assert requests[0].url.host == "cluster"
    assert requests[0].headers["authorization"] == "Bearer sk-profile"
    assert requests[0].url.params["zone_id"] == "sharedzone"


def test_explicit_endpoint_and_zone_override_profile(http_server):
    requests, _ = http_server
    save_cli_config(
        NexusCliConfig(
            current_profile="production",
            profiles={
                "production": ProfileEntry("http://wrong:2026", "sk-profile", "wrongzone"),
            },
        )
    )
    result = invoke(
        "stats",
        "--remote-url",
        "http://cluster:2027",
        "--remote-api-key",
        "sk-explicit",
        "--zone-id",
        "rightzone",
    )
    assert result.exit_code == 0, result.output
    assert requests[0].url.host == "cluster"
    assert requests[0].headers["authorization"] == "Bearer sk-explicit"
    assert requests[0].url.params["zone_id"] == "rightzone"


@pytest.mark.parametrize(
    "command,path",
    [
        (["query", "constellation", "--json"], "/v2/search/query"),
        (["index", "/docs"], "/v2/documents/index"),
        (["stats"], "/v2/documents/stats"),
    ],
)
@pytest.mark.parametrize(
    "status,body,code",
    [
        (401, {"error": "revoked key"}, 77),
        (403, {"error": "not allowed"}, 77),
        (503, {"error": "backend unavailable"}, 69),
        (200, {"results": [], "error": "index unavailable"}, 69),
    ],
)
def test_failures_never_report_success(http_server, command, path, status, body, code):
    requests, responses = http_server
    responses[path] = status, body
    result = invoke(*command, "--remote-url", "http://cluster:2027")
    assert result.exit_code == code, result.output
    assert "Indexing complete" not in result.output
    assert "No results found" not in result.output
    assert len(requests) == 1


def test_no_url_is_configuration_error(http_server):
    requests, _ = http_server
    result = invoke("stats")
    assert result.exit_code == 78, result.output
    assert requests == []


def test_limit_must_be_positive(http_server):
    requests, _ = http_server
    result = invoke("query", "constellation", "--limit", "0", "--remote-url", "http://cluster:2027")
    assert result.exit_code != 0
    assert requests == []


def test_empty_query_results_are_success(http_server):
    _, responses = http_server
    responses["/v2/search/query"] = 200, {"results": []}
    result = invoke("query", "missing", "--json", "--remote-url", "http://cluster:2027")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []
