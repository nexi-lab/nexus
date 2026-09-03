"""Unit tests for the read-your-writes fence dependency (Issue #4737)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Depends, FastAPI, Response
from fastapi.testclient import TestClient

from nexus.contracts.exceptions import NexusError
from nexus.lib.zone_revision import (
    MAX_REVISION_TIMEOUT_MS,
    MIN_REVISION_HEADER,
    REVISION_HEADER,
    REVISION_TIMEOUT_HEADER,
    reset_zone_revision_cache,
)
from nexus.server.api.v2._revision_fence import (
    RevisionFence,
    get_revision_fence,
    stamp_revision,
)


class FakeFS:
    """NexusFS stand-in: ``gens`` maps path → gen this node currently sees."""

    def __init__(self, gens: dict[str, int] | None = None, kernel: Any = None) -> None:
        self.gens = dict(gens or {})
        self._kernel = kernel
        self.stat_calls: list[tuple[str, Any]] = []

    def sys_stat(self, path: str, context: Any = None) -> dict[str, Any] | None:
        self.stat_calls.append((path, context))
        gen = self.gens.get(path)
        return None if gen is None else {"path": path, "gen": gen}


class FakeKernel:
    def __init__(self, applied: int = 0, *, exc: Exception | None = None) -> None:
        self.applied = applied
        self.exc = exc
        self.calls = 0

    def _call(self, method: str, params: dict[str, Any]) -> Any:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return {"zone_id": params["zone_id"], "has_store": True, "applied_index": self.applied}


_CTX = SimpleNamespace(user_id="alice")


def _make_app(fs: Any) -> FastAPI:
    app = FastAPI()
    app.state.fs = fs

    @app.get("/probe")
    async def probe(
        response: Response, fence: RevisionFence = Depends(get_revision_fence)
    ) -> dict[str, Any]:
        await fence.enforce(app.state.fs, context=_CTX)
        fence.stamp(response)
        return {"ok": True, "fenced": fence.active}

    return app


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    reset_zone_revision_cache()


# ---------------------------------------------------------------------------
# Path-anchored tokens (what writes return)
# ---------------------------------------------------------------------------


def test_unfenced_request_is_untouched() -> None:
    fs = FakeFS({"/a.txt": 10})
    client = TestClient(_make_app(fs))

    resp = client.get("/probe")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "fenced": False}
    assert REVISION_HEADER not in resp.headers
    assert fs.stat_calls == []


def test_path_fence_satisfied_stamps_observed_gen() -> None:
    fs = FakeFS({"/a.txt": 10})
    client = TestClient(_make_app(fs))

    resp = client.get("/probe", headers={MIN_REVISION_HEADER: "/a.txt@7"})

    assert resp.status_code == 200
    assert resp.json()["fenced"] is True
    assert resp.headers[REVISION_HEADER] == "/a.txt@10"
    # Stat went through the caller's context so permission hooks applied.
    assert fs.stat_calls == [("/a.txt", _CTX)]


def test_path_fence_via_query_param() -> None:
    client = TestClient(_make_app(FakeFS({"/a.txt": 10})))

    resp = client.get("/probe", params={"min_revision": "/a.txt@10"})

    assert resp.status_code == 200
    assert resp.headers[REVISION_HEADER] == "/a.txt@10"


def test_header_takes_precedence_over_query_param() -> None:
    client = TestClient(_make_app(FakeFS({"/a.txt": 10})))

    resp = client.get(
        "/probe",
        params={"min_revision": "/a.txt@99", "revision_timeout_ms": "0"},
        headers={MIN_REVISION_HEADER: "/a.txt@3"},
    )

    assert resp.status_code == 200


def test_path_not_yet_applied_returns_412_with_current_gen() -> None:
    fs = FakeFS({"/a.txt": 5})
    client = TestClient(_make_app(fs))

    resp = client.get(
        "/probe",
        headers={MIN_REVISION_HEADER: "/a.txt@9", REVISION_TIMEOUT_HEADER: "0"},
    )

    assert resp.status_code == 412
    detail = resp.json()["detail"]
    assert detail["error"] == "revision_not_applied"
    assert detail["min_revision"] == "/a.txt@9"
    assert detail["current_revision"] == "/a.txt@5"
    assert resp.headers[REVISION_HEADER] == "/a.txt@5"
    assert len(fs.stat_calls) == 1  # timeout 0 → single probe


def test_path_absent_on_this_node_is_412_not_metadata_none() -> None:
    client = TestClient(_make_app(FakeFS({})))

    resp = client.get(
        "/probe",
        headers={MIN_REVISION_HEADER: "/new.txt@1", REVISION_TIMEOUT_HEADER: "0"},
    )

    assert resp.status_code == 412
    assert resp.json()["detail"]["current_revision"] == "/new.txt@0"


def test_path_fence_waits_for_replication() -> None:
    class LaggingFS(FakeFS):
        def sys_stat(self, path: str, context: Any = None) -> dict[str, Any] | None:
            # Absent on the first two probes, then applied.
            self.stat_calls.append((path, context))
            return {"path": path, "gen": 4} if len(self.stat_calls) >= 3 else None

    fs = LaggingFS()
    client = TestClient(_make_app(fs))

    resp = client.get("/probe", headers={MIN_REVISION_HEADER: "/a.txt@4"})

    assert resp.status_code == 200
    assert resp.headers[REVISION_HEADER] == "/a.txt@4"
    assert len(fs.stat_calls) == 3


def test_missing_fs_returns_503() -> None:
    client = TestClient(_make_app(None))

    resp = client.get("/probe", headers={MIN_REVISION_HEADER: "/a.txt@1"})

    assert resp.status_code == 503


@pytest.mark.parametrize("raw", ["/a.txt@", "@5", "abc", "/a.txt@1.5", "root@-1"])
def test_malformed_min_revision_is_400(raw: str) -> None:
    client = TestClient(_make_app(FakeFS({"/a.txt": 1})))

    resp = client.get("/probe", headers={MIN_REVISION_HEADER: raw})

    assert resp.status_code == 400
    assert "Invalid X-Nexus-Min-Revision" in resp.json()["detail"]


@pytest.mark.parametrize("raw", ["-1", "abc", str(MAX_REVISION_TIMEOUT_MS + 1)])
def test_invalid_timeout_is_400(raw: str) -> None:
    client = TestClient(_make_app(FakeFS({"/a.txt": 1})))

    resp = client.get(
        "/probe", headers={MIN_REVISION_HEADER: "/a.txt@1", REVISION_TIMEOUT_HEADER: raw}
    )

    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Zone-anchored tokens (optional; kernel-stamped)
# ---------------------------------------------------------------------------


def test_zone_fence_satisfied_when_kernel_reports_applied_index() -> None:
    kernel = FakeKernel(applied=10)
    client = TestClient(_make_app(FakeFS(kernel=kernel)))

    resp = client.get("/probe", headers={MIN_REVISION_HEADER: "root@5"})

    assert resp.status_code == 200
    assert resp.headers[REVISION_HEADER] == "root@10"
    assert kernel.calls == 1


def test_bare_index_is_a_root_zone_token() -> None:
    kernel = FakeKernel(applied=10)
    client = TestClient(_make_app(FakeFS(kernel=kernel)))

    resp = client.get("/probe", headers={MIN_REVISION_HEADER: "99", REVISION_TIMEOUT_HEADER: "0"})

    assert resp.status_code == 412
    assert resp.json()["detail"]["current_revision"] == "root@10"


def test_zone_fence_on_pinned_kernel_returns_501() -> None:
    kernel = FakeKernel(exc=NexusError("unknown Call method: federation_cluster_info"))
    client = TestClient(_make_app(FakeFS(kernel=kernel)))

    resp = client.get("/probe", headers={MIN_REVISION_HEADER: "root@1"})

    assert resp.status_code == 501
    detail = resp.json()["detail"]
    assert detail["error"] == "zone_revision_unavailable"
    assert "path-anchored" in detail["message"]
    assert REVISION_HEADER not in resp.headers


def test_zone_probe_transport_error_returns_503() -> None:
    kernel = FakeKernel(exc=RuntimeError("connection refused"))
    client = TestClient(_make_app(FakeFS(kernel=kernel)))

    resp = client.get("/probe", headers={MIN_REVISION_HEADER: "root@1"})

    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "zone_revision_probe_failed"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_stamp_revision_sets_header_only_when_known() -> None:
    response = Response()
    stamp_revision(response, None)
    assert REVISION_HEADER not in response.headers
    stamp_revision(response, "/a.txt@3")
    assert response.headers[REVISION_HEADER] == "/a.txt@3"


def test_fence_stamp_is_noop_before_enforce() -> None:
    response = Response()
    RevisionFence().stamp(response)
    assert REVISION_HEADER not in response.headers
