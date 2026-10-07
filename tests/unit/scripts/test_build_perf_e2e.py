from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "test_build_perf_e2e.py"
SPEC = importlib.util.spec_from_file_location("test_build_perf_e2e_script", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_plan_auth_restore_requires_the_complete_original_line() -> None:
    assert MODULE._plan_auth_line_restored("# Plan\n- Configure authentication\n")
    assert not MODULE._plan_auth_line_restored("# Plan\nConfigure authentication\n")
    assert not MODULE._plan_auth_line_restored(
        "# Plan\n- Configure authentication\n- Configure auth (test-edit)\n"
    )


@pytest.mark.parametrize("key", [None, "viewer-key"])
def test_search_queries_the_mixed_http_api_with_caller_credentials(monkeypatch, key):
    requests = []
    hits = [{"path": "/workspace/demo/herb/customers/cust-002.md"}]
    monkeypatch.setattr(MODULE, "NEXUS_URL", "http://python-server:2026")
    monkeypatch.setattr(MODULE, "ADMIN_KEY", "admin-key")

    def respond(request, *, timeout):
        requests.append(request)
        response = io.BytesIO(json.dumps({"results": hits}).encode())
        response.status = 200
        assert timeout == 60
        return response

    monkeypatch.setattr(MODULE.urllib.request, "urlopen", respond)
    assert MODULE._search_results("auth + permissions & scope", limit=3, api_key=key) == hits
    request = requests[0]
    parsed = urlsplit(request.full_url)
    assert parsed.netloc == "python-server:2026"
    assert parsed.path == "/api/v2/search/query"
    assert parse_qs(parsed.query) == {
        "q": ["auth + permissions & scope"],
        "path": [MODULE.HERB_SEARCH_PATH],
        "type": ["hybrid"],
        "limit": ["3"],
    }
    assert request.get_header("Authorization") == f"Bearer {key or 'admin-key'}"


def test_search_backend_error_cannot_pass_a_quality_gate(monkeypatch):
    monkeypatch.setattr(
        MODULE,
        "_http",
        lambda *args, **kwargs: (
            200,
            {"error": "index unavailable", "results": [{"path": "expected"}]},
        ),
    )
    with pytest.raises(RuntimeError, match="index unavailable"):
        MODULE._search_results("expected")


def test_search_without_matches_is_a_successful_empty_result(monkeypatch):
    monkeypatch.setattr(MODULE, "_http", lambda *args, **kwargs: (200, {"results": []}))
    assert MODULE._search_results("absent") == []
