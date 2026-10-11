from __future__ import annotations

import importlib.util
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts/test_published_search_quality.py"
SPEC = importlib.util.spec_from_file_location("published_search_quality", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@contextmanager
def search_server(*, hits=8, status=200, error=None):
    requests = []
    expected = {case["question"]: case["expected_file"] for case in MODULE.HERB_QA_SET}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            query = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, query, self.headers.get("Authorization")))
            path = expected[query["q"]]
            body = {"results": [{"path": path}] if len(requests) <= hits else []}
            if error is not None:
                body["error"] = error
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_quality_gate_accepts_seven_hits_and_sends_authenticated_hybrid_queries():
    with search_server(hits=7) as (url, requests):
        MODULE.verify_quality(url, "operator-key")
    assert len(requests) == 8
    for path, query, authorization in requests:
        assert path == "/v2/search/query"
        assert authorization == "Bearer operator-key"
        assert query["path_filter"] == "/workspace/demo/herb"
        assert query["query_type"] == "hybrid" and query["limit"] == 5


def test_six_hits_fail_the_release_gate():
    with (
        search_server(hits=6) as (url, _),
        pytest.raises(AssertionError, match="requires 7 hits"),
    ):
        MODULE.verify_quality(url, "operator-key")


def test_success_status_with_backend_error_fails_even_if_the_expected_hit_is_present():
    with (
        search_server(error="index unavailable") as (url, _),
        pytest.raises(RuntimeError, match="index unavailable"),
    ):
        MODULE.verify_quality(url, "operator-key")


def test_http_failure_does_not_become_a_quality_miss():
    with search_server(status=503) as (url, _):
        with pytest.raises(HTTPError) as failure:
            MODULE.verify_quality(url, "operator-key")
        assert failure.value.code == 503
