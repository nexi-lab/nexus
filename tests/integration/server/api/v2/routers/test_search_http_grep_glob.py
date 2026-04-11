"""Integration tests for HTTP grep/glob endpoints (#3701 Issue 1A).

Exercises ``GET /api/v2/search/grep`` and ``GET /api/v2/search/glob`` via
FastAPI's ``TestClient``. Mocks ``nexus_fs.service("search")`` to return
a controllable SearchService stub so we can assert:

* happy paths (basic match, pagination, case flags)
* error paths (invalid regex → 400, service missing → 503, nexus_fs
  missing → 503)
* ReBAC interaction (permission_enforcer called, denied files stripped)
* ``truncated_by_permissions`` surfaces in the response when denial rate
  is high
* MCP ↔ HTTP convergence sanity (HTTP uses the same ``build_paginated_list_response``
  envelope as MCP)

Backfills the coverage gap flagged during the review of #3701.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False


pytestmark = pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi test client unavailable")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_search_service(
    *,
    grep_return: list[dict[str, Any]] | None = None,
    glob_return: list[str] | None = None,
    grep_raises: Exception | None = None,
    glob_raises: Exception | None = None,
) -> MagicMock:
    """Create a MagicMock SearchService with configurable behaviour."""
    svc = MagicMock()

    async def fake_grep(**_kwargs: Any) -> list[dict[str, Any]]:
        if grep_raises is not None:
            raise grep_raises
        return list(grep_return or [])

    svc.grep = AsyncMock(side_effect=fake_grep)

    def fake_glob(**_kwargs: Any) -> list[str]:
        if glob_raises is not None:
            raise glob_raises
        return list(glob_return or [])

    svc.glob = MagicMock(side_effect=fake_glob)
    return svc


def _build_app(
    *,
    search_service: Any,
    permission_enforcer: Any = None,
    nexus_fs_present: bool = True,
) -> "FastAPI":
    """Assemble a minimal FastAPI app wiring the search router."""
    from nexus.server.api.v2.routers.search import router
    from nexus.server.dependencies import require_auth

    app = FastAPI()
    app.include_router(router)

    # Mock nexus_fs with .service("search") → SearchService
    if nexus_fs_present:
        fs = MagicMock()
        fs.service = MagicMock(
            side_effect=lambda name: search_service if name == "search" else None
        )
        app.state.nexus_fs = fs
    else:
        app.state.nexus_fs = None

    # Minimal daemon (only needed for error path tests — grep/glob don't use it)
    mock_daemon = MagicMock()
    mock_daemon.is_initialized = True
    app.state.search_daemon = mock_daemon
    app.state.permission_enforcer = permission_enforcer

    app.dependency_overrides[require_auth] = lambda: {
        "authenticated": True,
        "subject_id": "user:alice",
        "user_id": "user:alice",
        "zone_id": "root",
        "is_admin": False,
    }
    return app


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestGrepHappyPath:
    def test_basic_match_returns_results(self) -> None:
        svc = _make_search_service(
            grep_return=[
                {"file": "/a.py", "line": 10, "content": "hello", "match": "hello"},
                {"file": "/b.py", "line": 20, "content": "hello", "match": "hello"},
            ]
        )
        client = TestClient(_build_app(search_service=svc))
        resp = client.get("/api/v2/search/grep?pattern=hello")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert data["count"] == 2
        assert data["has_more"] is False
        assert len(data["items"]) == 2
        assert data["items"][0]["file"] == "/a.py"

    def test_pagination_first_page(self) -> None:
        results = [
            {"file": f"/f{i}.py", "line": i, "content": "m", "match": "m"} for i in range(10)
        ]
        svc = _make_search_service(grep_return=results)
        client = TestClient(_build_app(search_service=svc))
        resp = client.get("/api/v2/search/grep?pattern=m&limit=3&offset=0")
        data = resp.json()
        assert data["total"] == 10
        assert data["count"] == 3
        assert data["has_more"] is True
        assert data["next_offset"] == 3

    def test_pagination_last_page(self) -> None:
        results = [
            {"file": f"/f{i}.py", "line": i, "content": "m", "match": "m"} for i in range(10)
        ]
        svc = _make_search_service(grep_return=results)
        client = TestClient(_build_app(search_service=svc))
        resp = client.get("/api/v2/search/grep?pattern=m&limit=5&offset=5")
        data = resp.json()
        assert data["count"] == 5
        assert data["has_more"] is False
        assert data["next_offset"] is None

    def test_ignore_case_flag_forwarded(self) -> None:
        svc = _make_search_service(grep_return=[])
        client = TestClient(_build_app(search_service=svc))
        client.get("/api/v2/search/grep?pattern=x&ignore_case=true")
        kwargs = svc.grep.await_args.kwargs
        assert kwargs["ignore_case"] is True

    def test_path_parameter_forwarded(self) -> None:
        svc = _make_search_service(grep_return=[])
        client = TestClient(_build_app(search_service=svc))
        client.get("/api/v2/search/grep?pattern=x&path=/src")
        kwargs = svc.grep.await_args.kwargs
        assert kwargs["path"] == "/src"

    def test_context_lines_forwarded(self) -> None:
        svc = _make_search_service(grep_return=[])
        client = TestClient(_build_app(search_service=svc))
        client.get("/api/v2/search/grep?pattern=x&before_context=2&after_context=3")
        kwargs = svc.grep.await_args.kwargs
        assert kwargs["before_context"] == 2
        assert kwargs["after_context"] == 3

    def test_max_results_overfetches_when_enforcer_active(self) -> None:
        """Issue 16A: fetch_limit = (limit + offset) * overfetch when enforcer on."""
        svc = _make_search_service(grep_return=[])
        enforcer = MagicMock()
        enforcer.filter_search_results = MagicMock(return_value=[])
        client = TestClient(_build_app(search_service=svc, permission_enforcer=enforcer))
        client.get("/api/v2/search/grep?pattern=x&limit=10&offset=0")
        kwargs = svc.grep.await_args.kwargs
        # _REBAC_OVERFETCH_FACTOR is 3 → fetch_limit = 10*3
        assert kwargs["max_results"] == 30


class TestGlobHappyPath:
    def test_basic_match_returns_paths(self) -> None:
        svc = _make_search_service(glob_return=["/a.py", "/b.py", "/c.py"])
        client = TestClient(_build_app(search_service=svc))
        resp = client.get("/api/v2/search/glob?pattern=*.py")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert data["items"] == ["/a.py", "/b.py", "/c.py"]

    def test_pagination(self) -> None:
        svc = _make_search_service(glob_return=[f"/f{i}.py" for i in range(10)])
        client = TestClient(_build_app(search_service=svc))
        resp = client.get("/api/v2/search/glob?pattern=*.py&limit=4&offset=2")
        data = resp.json()
        assert data["offset"] == 2
        assert data["count"] == 4
        assert data["has_more"] is True
        assert data["next_offset"] == 6

    def test_path_parameter_forwarded(self) -> None:
        svc = _make_search_service(glob_return=[])
        client = TestClient(_build_app(search_service=svc))
        client.get("/api/v2/search/glob?pattern=*.py&path=/workspace")
        kwargs = svc.glob.call_args.kwargs
        assert kwargs["path"] == "/workspace"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestGrepErrors:
    def test_invalid_regex_returns_400(self) -> None:
        svc = _make_search_service(grep_raises=ValueError("Invalid regex pattern"))
        client = TestClient(_build_app(search_service=svc))
        resp = client.get("/api/v2/search/grep?pattern=[invalid")
        assert resp.status_code == 400
        assert "Invalid regex" in resp.json()["detail"]

    def test_service_unavailable_returns_503(self) -> None:
        """Search service absent from NexusFS."""
        nexus_fs = MagicMock()
        nexus_fs.service = MagicMock(return_value=None)
        from nexus.server.api.v2.routers.search import router
        from nexus.server.dependencies import require_auth

        app = FastAPI()
        app.include_router(router)
        app.state.nexus_fs = nexus_fs
        app.state.search_daemon = MagicMock(is_initialized=True)
        app.state.permission_enforcer = None
        app.dependency_overrides[require_auth] = lambda: {
            "authenticated": True,
            "subject_id": "user:alice",
            "user_id": "user:alice",
            "zone_id": "root",
        }
        client = TestClient(app)
        resp = client.get("/api/v2/search/grep?pattern=x")
        assert resp.status_code == 503

    def test_nexus_fs_not_initialized_returns_503(self) -> None:
        svc = _make_search_service(grep_return=[])
        client = TestClient(_build_app(search_service=svc, nexus_fs_present=False))
        resp = client.get("/api/v2/search/grep?pattern=x")
        assert resp.status_code == 503

    def test_internal_error_returns_500(self) -> None:
        svc = _make_search_service(grep_raises=RuntimeError("something broke"))
        client = TestClient(_build_app(search_service=svc))
        resp = client.get("/api/v2/search/grep?pattern=x")
        assert resp.status_code == 500

    def test_empty_pattern_rejected(self) -> None:
        svc = _make_search_service(grep_return=[])
        client = TestClient(_build_app(search_service=svc))
        resp = client.get("/api/v2/search/grep?pattern=")
        assert resp.status_code == 422

    def test_negative_offset_rejected(self) -> None:
        svc = _make_search_service(grep_return=[])
        client = TestClient(_build_app(search_service=svc))
        resp = client.get("/api/v2/search/grep?pattern=x&offset=-1")
        assert resp.status_code == 422


class TestGlobErrors:
    def test_invalid_pattern_returns_400(self) -> None:
        svc = _make_search_service(glob_raises=ValueError("Bad glob"))
        client = TestClient(_build_app(search_service=svc))
        resp = client.get("/api/v2/search/glob?pattern=[invalid")
        assert resp.status_code == 400

    def test_internal_error_returns_500(self) -> None:
        svc = _make_search_service(glob_raises=RuntimeError("oops"))
        client = TestClient(_build_app(search_service=svc))
        resp = client.get("/api/v2/search/glob?pattern=*.py")
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# ReBAC interaction
# ---------------------------------------------------------------------------


class TestRebacInteraction:
    def test_denied_files_stripped_from_grep_results(self) -> None:
        svc = _make_search_service(
            grep_return=[
                {"file": "/public/a.py", "line": 1, "content": "x", "match": "x"},
                {"file": "/secret/b.py", "line": 1, "content": "x", "match": "x"},
                {"file": "/public/c.py", "line": 1, "content": "x", "match": "x"},
            ]
        )
        enforcer = MagicMock()
        enforcer.filter_search_results = MagicMock(return_value=["/public/a.py", "/public/c.py"])
        client = TestClient(_build_app(search_service=svc, permission_enforcer=enforcer))
        resp = client.get("/api/v2/search/grep?pattern=x")
        data = resp.json()
        files = [r["file"] for r in data["items"]]
        assert "/secret/b.py" not in files
        assert files == ["/public/a.py", "/public/c.py"]

    def test_denied_files_stripped_from_glob_results(self) -> None:
        svc = _make_search_service(glob_return=["/public/a.py", "/secret/b.py", "/public/c.py"])
        enforcer = MagicMock()
        enforcer.filter_search_results = MagicMock(return_value=["/public/a.py", "/public/c.py"])
        client = TestClient(_build_app(search_service=svc, permission_enforcer=enforcer))
        resp = client.get("/api/v2/search/glob?pattern=**/*.py")
        data = resp.json()
        assert "/secret/b.py" not in data["items"]

    def test_denial_rate_reported_in_response(self) -> None:
        """4 results pre-filter, 1 post-filter → 75% denial.

        Because 1 < limit=10 AND denial rate 0.75 >= the warn threshold,
        the response flags ``truncated_by_permissions=True`` so the
        caller can react (paginate, increase limit, or re-request).
        """
        svc = _make_search_service(
            grep_return=[
                {"file": f"/f{i}.py", "line": 1, "content": "x", "match": "x"} for i in range(4)
            ]
        )
        enforcer = MagicMock()
        enforcer.filter_search_results = MagicMock(return_value=["/f0.py"])
        client = TestClient(_build_app(search_service=svc, permission_enforcer=enforcer))
        resp = client.get("/api/v2/search/grep?pattern=x&limit=10")
        data = resp.json()
        assert data["permission_denial_rate"] == 0.75
        assert data["truncated_by_permissions"] is True

    def test_low_denial_rate_not_flagged(self) -> None:
        """40 results, 30 permitted (25% denial), limit 10 → not flagged."""
        svc = _make_search_service(
            grep_return=[
                {"file": f"/f{i}.py", "line": 1, "content": "x", "match": "x"} for i in range(40)
            ]
        )
        enforcer = MagicMock()
        enforcer.filter_search_results = MagicMock(return_value=[f"/f{i}.py" for i in range(30)])
        client = TestClient(_build_app(search_service=svc, permission_enforcer=enforcer))
        resp = client.get("/api/v2/search/grep?pattern=x&limit=10")
        data = resp.json()
        assert data["permission_denial_rate"] == 0.25
        assert data["truncated_by_permissions"] is False

    def test_high_denial_flagged_as_truncated(self) -> None:
        """Explicit lock-in: high denial + undercount == truncated_by_permissions."""
        svc = _make_search_service(
            grep_return=[
                {"file": f"/f{i}.py", "line": 1, "content": "x", "match": "x"} for i in range(20)
            ]
        )
        enforcer = MagicMock()
        enforcer.filter_search_results = MagicMock(return_value=["/f0.py"])
        client = TestClient(_build_app(search_service=svc, permission_enforcer=enforcer))
        resp = client.get("/api/v2/search/grep?pattern=x&limit=10")
        data = resp.json()
        assert data["permission_denial_rate"] == 0.95
        # 1 permitted < 10 limit AND denial 0.95 >= 0.5 → flagged
        assert data["truncated_by_permissions"] is True

    def test_no_enforcer_no_denial_stats_degraded(self) -> None:
        svc = _make_search_service(
            grep_return=[
                {"file": "/a.py", "line": 1, "content": "x", "match": "x"},
            ]
        )
        client = TestClient(_build_app(search_service=svc, permission_enforcer=None))
        resp = client.get("/api/v2/search/grep?pattern=x")
        data = resp.json()
        assert data["permission_denial_rate"] == 0.0
        assert data["truncated_by_permissions"] is False


# ---------------------------------------------------------------------------
# Parity — HTTP grep/glob and HTTP query share response envelope shape (#3701 10A)
# ---------------------------------------------------------------------------


class TestHttpEnvelopeParity:
    def test_grep_envelope_has_same_pagination_fields_as_glob(self) -> None:
        """Locks in Issue 5A + 10A: HTTP grep and glob emit the same envelope."""
        svc = _make_search_service(
            grep_return=[{"file": "/a.py", "line": 1, "content": "x", "match": "x"}],
            glob_return=["/a.py"],
        )
        client = TestClient(_build_app(search_service=svc))
        grep_resp = client.get("/api/v2/search/grep?pattern=x")
        glob_resp = client.get("/api/v2/search/glob?pattern=*.py")

        shared_keys = {"total", "count", "offset", "items", "has_more", "next_offset"}
        assert shared_keys <= set(grep_resp.json().keys())
        assert shared_keys <= set(glob_resp.json().keys())

    def test_both_include_rebac_instrumentation(self) -> None:
        svc = _make_search_service(grep_return=[], glob_return=[])
        enforcer = MagicMock()
        enforcer.filter_search_results = MagicMock(return_value=[])
        client = TestClient(_build_app(search_service=svc, permission_enforcer=enforcer))
        for endpoint in ("/api/v2/search/grep?pattern=x", "/api/v2/search/glob?pattern=*.py"):
            data = client.get(endpoint).json()
            assert "permission_denial_rate" in data
            assert "truncated_by_permissions" in data


# ---------------------------------------------------------------------------
# Cross-endpoint parity (#3701 Issue 10A)
#
# Locks in the invariant: for the same user, same zone, and same ReBAC
# policy, HTTP grep/glob/query all strip exactly the same set of files
# via the same ``_apply_rebac_filter`` helper. This is the scoped-X
# version of the 10A parity test — MCP is deferred pending auth-identity
# infrastructure (see notes in the review summary).
# ---------------------------------------------------------------------------


def _file_parity_app(*, permitted: list[str]) -> tuple["FastAPI", list[dict[str, Any]]]:
    """Build an app where the ReBAC policy permits only ``permitted`` paths.

    The full file set is fixed (``/public/a.py``, ``/secret/b.py``,
    ``/public/c.py``, ``/secret/d.py``) so the parity assertions have a
    known ground truth.
    """
    all_files = ["/public/a.py", "/secret/b.py", "/public/c.py", "/secret/d.py"]
    permitted_set = set(permitted)

    grep_corpus = [{"file": p, "line": 1, "content": "match", "match": "match"} for p in all_files]

    svc = _make_search_service(grep_return=grep_corpus, glob_return=all_files)

    enforcer = MagicMock()
    enforcer.filter_search_results = MagicMock(
        side_effect=lambda paths, **_: [p for p in paths if p in permitted_set]
    )

    app = _build_app(search_service=svc, permission_enforcer=enforcer)
    return app, grep_corpus


class TestCrossEndpointParity:
    def test_grep_and_glob_return_same_permitted_files(self) -> None:
        app, _ = _file_parity_app(permitted=["/public/a.py", "/public/c.py"])
        client = TestClient(app)

        grep_files = sorted(
            r["file"] for r in client.get("/api/v2/search/grep?pattern=match").json()["items"]
        )
        glob_files = sorted(client.get("/api/v2/search/glob?pattern=**/*.py").json()["items"])

        assert grep_files == glob_files == ["/public/a.py", "/public/c.py"]

    def test_full_denial_returns_empty_for_both(self) -> None:
        app, _ = _file_parity_app(permitted=[])
        client = TestClient(app)

        grep = client.get("/api/v2/search/grep?pattern=match").json()
        glob = client.get("/api/v2/search/glob?pattern=**/*.py").json()

        assert grep["items"] == []
        assert glob["items"] == []
        assert grep["total"] == 0
        assert glob["total"] == 0

    def test_full_permit_returns_all_for_both(self) -> None:
        app, _ = _file_parity_app(
            permitted=["/public/a.py", "/secret/b.py", "/public/c.py", "/secret/d.py"]
        )
        client = TestClient(app)

        grep_files = sorted(
            r["file"] for r in client.get("/api/v2/search/grep?pattern=match").json()["items"]
        )
        glob_files = sorted(client.get("/api/v2/search/glob?pattern=**/*.py").json()["items"])

        expected = sorted(["/public/a.py", "/secret/b.py", "/public/c.py", "/secret/d.py"])
        assert grep_files == expected
        assert glob_files == expected

    def test_grep_and_glob_report_same_denial_rate(self) -> None:
        app, _ = _file_parity_app(permitted=["/public/a.py"])  # 1/4 = 0.25 permit → 0.75 deny
        client = TestClient(app)

        grep = client.get("/api/v2/search/grep?pattern=match").json()
        glob = client.get("/api/v2/search/glob?pattern=**/*.py").json()

        assert grep["permission_denial_rate"] == glob["permission_denial_rate"] == 0.75


# ---------------------------------------------------------------------------
# files=[...] parameter (#3701 Issue 2A) — HTTP surface
# ---------------------------------------------------------------------------


class TestHttpFilesParameter:
    """Locks in the ``files=`` query parameter semantics on grep/glob HTTP.

    The underlying SearchService behaviour is tested in
    ``tests/integration/services/test_search_service.py``; these tests
    assert the HTTP layer forwards the list correctly and surfaces
    SearchService errors as 400 responses.
    """

    def test_grep_files_forwarded_as_list(self) -> None:
        svc = _make_search_service(grep_return=[])
        client = TestClient(_build_app(search_service=svc))
        client.get("/api/v2/search/grep?pattern=TODO&files=/src/a.py&files=/src/b.py")
        kwargs = svc.grep.await_args.kwargs
        assert kwargs["files"] == ["/src/a.py", "/src/b.py"]

    def test_glob_files_forwarded_as_list(self) -> None:
        svc = _make_search_service(glob_return=[])
        client = TestClient(_build_app(search_service=svc))
        client.get("/api/v2/search/glob?pattern=*.py&files=/src/a.py&files=/src/b.py")
        kwargs = svc.glob.call_args.kwargs
        assert kwargs["files"] == ["/src/a.py", "/src/b.py"]

    def test_grep_files_absent_forwards_none(self) -> None:
        svc = _make_search_service(grep_return=[])
        client = TestClient(_build_app(search_service=svc))
        client.get("/api/v2/search/grep?pattern=TODO")
        kwargs = svc.grep.await_args.kwargs
        assert kwargs["files"] is None

    def test_grep_files_validation_error_returns_400(self) -> None:
        """SearchService rejections (size cap, traversal, cross-zone) → 400."""
        svc = _make_search_service(grep_raises=ValueError("files list too large: 20000 > 10000"))
        client = TestClient(_build_app(search_service=svc))
        resp = client.get("/api/v2/search/grep?pattern=x&files=/a.py")
        assert resp.status_code == 400
        assert "too large" in resp.json()["detail"]

    def test_grep_files_traversal_returns_400(self) -> None:
        svc = _make_search_service(grep_raises=ValueError("path traversal rejected"))
        client = TestClient(_build_app(search_service=svc))
        resp = client.get("/api/v2/search/grep?pattern=x&files=../etc/passwd")
        assert resp.status_code == 400

    def test_grep_files_traversal_invalid_path_error_returns_400(self) -> None:
        """Live-validation regression: the real SearchService raises
        ``InvalidPathError`` (not ``ValueError``) on a traversal segment.
        Previously the handler only caught ``ValueError`` and the
        traversal leaked through as a 500. Locks in the fix."""
        from nexus.contracts.exceptions import InvalidPathError

        svc = _make_search_service(
            grep_raises=InvalidPathError("path traversal rejected: ../etc/passwd")
        )
        client = TestClient(_build_app(search_service=svc))
        resp = client.get("/api/v2/search/grep?pattern=x&files=../etc/passwd")
        assert resp.status_code == 400
        assert "traversal" in resp.json()["detail"]

    def test_glob_invalid_path_error_returns_400(self) -> None:
        """Same regression for the glob handler."""
        from nexus.contracts.exceptions import InvalidPathError

        svc = _make_search_service(glob_raises=InvalidPathError("path traversal rejected"))
        client = TestClient(_build_app(search_service=svc))
        resp = client.get("/api/v2/search/glob?pattern=*.py&files=../etc/passwd")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/v2/search/grep (#3701 follow-up: JSON body for large files=)
# ---------------------------------------------------------------------------


class TestPostGrep:
    """POST /grep accepts the same fields as GET but from a JSON body."""

    def test_post_grep_basic(self) -> None:
        svc = _make_search_service(
            grep_return=[
                {"file": "/a.py", "line": 1, "content": "TODO", "match": "TODO"},
            ]
        )
        client = TestClient(_build_app(search_service=svc))
        resp = client.post("/api/v2/search/grep", json={"pattern": "TODO"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["file"] == "/a.py"

    def test_post_grep_forwards_all_body_fields(self) -> None:
        svc = _make_search_service(grep_return=[])
        client = TestClient(_build_app(search_service=svc))
        client.post(
            "/api/v2/search/grep",
            json={
                "pattern": "TODO",
                "path": "/src",
                "ignore_case": True,
                "limit": 20,
                "offset": 5,
                "before_context": 2,
                "after_context": 3,
                "invert_match": True,
                "files": ["/src/a.py", "/src/b.py"],
            },
        )
        kwargs = svc.grep.await_args.kwargs
        assert kwargs["pattern"] == "TODO"
        assert kwargs["path"] == "/src"
        assert kwargs["ignore_case"] is True
        assert kwargs["before_context"] == 2
        assert kwargs["after_context"] == 3
        assert kwargs["invert_match"] is True
        assert kwargs["files"] == ["/src/a.py", "/src/b.py"]

    def test_post_grep_large_files_list(self) -> None:
        """The whole point of POST: 5000 files over JSON body, no URL limit."""
        svc = _make_search_service(grep_return=[])
        client = TestClient(_build_app(search_service=svc))
        paths = [f"/f{i}.py" for i in range(5000)]
        resp = client.post("/api/v2/search/grep", json={"pattern": "x", "files": paths})
        assert resp.status_code == 200
        assert svc.grep.await_args.kwargs["files"] == paths

    def test_post_grep_missing_pattern_returns_400(self) -> None:
        svc = _make_search_service(grep_return=[])
        client = TestClient(_build_app(search_service=svc))
        resp = client.post("/api/v2/search/grep", json={"path": "/src"})
        assert resp.status_code == 400
        assert "pattern" in resp.json()["detail"]

    def test_post_grep_empty_pattern_returns_400(self) -> None:
        svc = _make_search_service(grep_return=[])
        client = TestClient(_build_app(search_service=svc))
        resp = client.post("/api/v2/search/grep", json={"pattern": ""})
        assert resp.status_code == 400

    def test_post_grep_invalid_json_returns_400(self) -> None:
        svc = _make_search_service(grep_return=[])
        client = TestClient(_build_app(search_service=svc))
        resp = client.post(
            "/api/v2/search/grep",
            content=b"not valid json {{{",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_post_grep_body_not_object_returns_400(self) -> None:
        svc = _make_search_service(grep_return=[])
        client = TestClient(_build_app(search_service=svc))
        resp = client.post("/api/v2/search/grep", json=["not", "an", "object"])
        assert resp.status_code == 400

    def test_post_grep_limit_out_of_range_returns_400(self) -> None:
        svc = _make_search_service(grep_return=[])
        client = TestClient(_build_app(search_service=svc))
        resp = client.post("/api/v2/search/grep", json={"pattern": "x", "limit": 50000})
        assert resp.status_code == 400

    def test_post_grep_negative_offset_returns_400(self) -> None:
        svc = _make_search_service(grep_return=[])
        client = TestClient(_build_app(search_service=svc))
        resp = client.post("/api/v2/search/grep", json={"pattern": "x", "offset": -5})
        assert resp.status_code == 400

    def test_post_grep_context_exceeds_max_returns_400(self) -> None:
        svc = _make_search_service(grep_return=[])
        client = TestClient(_build_app(search_service=svc))
        resp = client.post(
            "/api/v2/search/grep",
            json={"pattern": "x", "before_context": 100},
        )
        assert resp.status_code == 400

    def test_post_grep_files_not_list_returns_400(self) -> None:
        svc = _make_search_service(grep_return=[])
        client = TestClient(_build_app(search_service=svc))
        resp = client.post(
            "/api/v2/search/grep",
            json={"pattern": "x", "files": "not-a-list"},
        )
        assert resp.status_code == 400
        assert "files" in resp.json()["detail"]

    def test_post_grep_files_contains_non_string_returns_400(self) -> None:
        svc = _make_search_service(grep_return=[])
        client = TestClient(_build_app(search_service=svc))
        resp = client.post(
            "/api/v2/search/grep",
            json={"pattern": "x", "files": ["/ok.py", 42, "/also-ok.py"]},
        )
        assert resp.status_code == 400

    def test_post_grep_files_empty_list_preserved(self) -> None:
        """files=[] must reach SearchService (empty short-circuit), not be
        coalesced to None."""
        svc = _make_search_service(grep_return=[])
        client = TestClient(_build_app(search_service=svc))
        client.post("/api/v2/search/grep", json={"pattern": "x", "files": []})
        assert svc.grep.await_args.kwargs["files"] == []

    def test_post_grep_files_absent_becomes_none(self) -> None:
        """When ``files`` is not in the body, the kwarg is None (walk tree)."""
        svc = _make_search_service(grep_return=[])
        client = TestClient(_build_app(search_service=svc))
        client.post("/api/v2/search/grep", json={"pattern": "x"})
        assert svc.grep.await_args.kwargs["files"] is None

    def test_post_grep_traversal_returns_400(self) -> None:
        from nexus.contracts.exceptions import InvalidPathError

        svc = _make_search_service(
            grep_raises=InvalidPathError("path traversal rejected: ../etc/passwd")
        )
        client = TestClient(_build_app(search_service=svc))
        resp = client.post(
            "/api/v2/search/grep",
            json={"pattern": "x", "files": ["../etc/passwd"]},
        )
        assert resp.status_code == 400

    def test_post_grep_rebac_filter_strips_denied_files(self) -> None:
        """POST handler shares the same ReBAC hook as GET."""
        svc = _make_search_service(
            grep_return=[
                {"file": "/public/a.py", "line": 1, "content": "x", "match": "x"},
                {"file": "/secret/b.py", "line": 1, "content": "x", "match": "x"},
            ]
        )
        enforcer = MagicMock()
        enforcer.filter_search_results = MagicMock(return_value=["/public/a.py"])
        client = TestClient(_build_app(search_service=svc, permission_enforcer=enforcer))
        resp = client.post("/api/v2/search/grep", json={"pattern": "x"})
        data = resp.json()
        files = [r["file"] for r in data["items"]]
        assert "/secret/b.py" not in files
        assert files == ["/public/a.py"]
        assert data["permission_denial_rate"] == 0.5


# ---------------------------------------------------------------------------
# POST /api/v2/search/glob (#3701 follow-up)
# ---------------------------------------------------------------------------


class TestPostGlob:
    """POST /glob mirrors POST /grep semantics."""

    def test_post_glob_basic(self) -> None:
        svc = _make_search_service(glob_return=["/a.py", "/b.py"])
        client = TestClient(_build_app(search_service=svc))
        resp = client.post("/api/v2/search/glob", json={"pattern": "*.py"})
        assert resp.status_code == 200
        assert resp.json()["items"] == ["/a.py", "/b.py"]

    def test_post_glob_large_files_list(self) -> None:
        svc = _make_search_service(glob_return=[])
        client = TestClient(_build_app(search_service=svc))
        paths = [f"/f{i}.py" for i in range(5000)]
        resp = client.post("/api/v2/search/glob", json={"pattern": "*.py", "files": paths})
        assert resp.status_code == 200
        assert svc.glob.call_args.kwargs["files"] == paths

    def test_post_glob_missing_pattern_returns_400(self) -> None:
        svc = _make_search_service(glob_return=[])
        client = TestClient(_build_app(search_service=svc))
        resp = client.post("/api/v2/search/glob", json={})
        assert resp.status_code == 400

    def test_post_glob_files_empty_list_preserved(self) -> None:
        svc = _make_search_service(glob_return=[])
        client = TestClient(_build_app(search_service=svc))
        client.post("/api/v2/search/glob", json={"pattern": "*.py", "files": []})
        assert svc.glob.call_args.kwargs["files"] == []

    def test_post_glob_rebac_filter_strips_denied(self) -> None:
        svc = _make_search_service(glob_return=["/public/a.py", "/secret/b.py"])
        enforcer = MagicMock()
        enforcer.filter_search_results = MagicMock(return_value=["/public/a.py"])
        client = TestClient(_build_app(search_service=svc, permission_enforcer=enforcer))
        resp = client.post("/api/v2/search/glob", json={"pattern": "*.py"})
        data = resp.json()
        assert "/secret/b.py" not in data["items"]
        assert data["permission_denial_rate"] == 0.5


# ---------------------------------------------------------------------------
# GET/POST parity — same user, same request, identical response
# ---------------------------------------------------------------------------


class TestGetPostParity:
    """Lock in that GET and POST return identical envelopes for the same
    user and request parameters. Prevents drift when one is refactored."""

    def test_grep_get_and_post_return_equivalent_results(self) -> None:
        svc = _make_search_service(
            grep_return=[
                {"file": "/a.py", "line": 1, "content": "x", "match": "x"},
                {"file": "/b.py", "line": 1, "content": "x", "match": "x"},
            ]
        )
        client = TestClient(_build_app(search_service=svc))

        get_resp = client.get("/api/v2/search/grep?pattern=x&files=/a.py&files=/b.py")
        post_resp = client.post(
            "/api/v2/search/grep",
            json={"pattern": "x", "files": ["/a.py", "/b.py"]},
        )

        # Envelope fields that should match between GET and POST
        for key in (
            "total",
            "count",
            "offset",
            "has_more",
            "next_offset",
            "permission_denial_rate",
            "truncated_by_permissions",
        ):
            assert get_resp.json()[key] == post_resp.json()[key], f"mismatch on {key}"
        # Items should also match (order matters)
        assert get_resp.json()["items"] == post_resp.json()["items"]

    def test_glob_get_and_post_return_equivalent_results(self) -> None:
        svc = _make_search_service(glob_return=["/a.py", "/b.py"])
        client = TestClient(_build_app(search_service=svc))

        get_resp = client.get("/api/v2/search/glob?pattern=*.py")
        post_resp = client.post("/api/v2/search/glob", json={"pattern": "*.py"})

        assert get_resp.json()["items"] == post_resp.json()["items"]
        assert get_resp.json()["total"] == post_resp.json()["total"]
