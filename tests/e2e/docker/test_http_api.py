"""HTTP-API E2E — locks the R10 wiring against a REAL nexusd-cluster.

The R10 arc lands a pure-Rust HTTP surface (`rust/services/http-api/`)
that a `--features http-api`-featured `nexusd-cluster` binds via
`NEXUS_HTTP_ADDR`.  This suite spins up the single-node compose in
`dockerfiles/docker-compose.http-api-e2e.yml` and drives the real
axum listener over real TCP with a real minted `sk-*` key against a
real `ApiKeyAuthProvider` — the acceptance test the crate's own
unit tests + auth-middleware E2E cannot cover (all their auth
providers are stubs).

## Scenarios

  * `test_status_is_public_without_auth` — the `/v2/status` sub-router
    is deliberately outside the bearer middleware so liveness probes
    (kube, docker healthchecks) work before any bearer exists.
  * `test_search_query_without_bearer_returns_401` — proves the
    middleware is wired at the composition root (not just imported).
    Without wiring, `ApiKeyAuthProvider` never runs and the request
    reaches the upstream search backend, which would return an
    unauthenticated-but-unhelpful status.
  * `test_search_query_with_wrong_bearer_returns_401` — proves the
    provider actually resolves against the minted key store.  A
    stub-that-accepts-anything would 200-through here.
  * `test_search_query_with_valid_bearer_is_not_401` — proves the
    minted key is admitted end-to-end.  The status is intentionally
    "NOT 401" rather than "200" because this compose does not bake
    the search-plugin cdylib (deliberately — testing the auth plane,
    not the search chain), so the upstream `SearchService` returns
    Unimplemented → HTTP 501 or Unavailable → 503.  Both count as
    "auth admitted, backend downstream." Real search happy-path with
    a real plugin lives in `test_search_plugin.py`.

## The stdlib-only choice

Uses `urllib.request` rather than `requests` / `httpx` so the test
runs on the shared `nexus-federation-test` image without a bespoke
`pip install` layer — same reason `test_search_plugin.py` uses raw
grpc rather than reaching for anything higher-level.
"""

from __future__ import annotations

import json
import os
import pathlib
import urllib.error
import urllib.request

import pytest

from tests.e2e.docker.runbook_helpers import wait_healthy

pytestmark = [
    pytest.mark.xdist_group("http-api-e2e"),
    pytest.mark.skipif(
        os.environ.get("NEXUS_HTTP_API_E2E") != "1",
        reason="HTTP-API E2E suite needs the docker-compose.http-api-e2e stack; "
        "set NEXUS_HTTP_API_E2E=1 to enable (CI sets this automatically).",
    ),
]


HTTP_URL = os.environ.get("NEXUS_HTTP_URL", "http://node:2128")
KEY_FILE = os.environ.get("NEXUS_HTTP_KEY_FILE", "/shared/sk-key")
# gRPC port on the same daemon — used only for the readiness gate (TCP
# probe on 2126 has been the convention across every sibling suite;
# 2128 is HTTP so we use `wait_healthy` on the gRPC port and then rely
# on the compose healthcheck for the axum listener).
NODE_GRPC = os.environ.get("NEXUS_HTTP_NODE_GRPC", "node:2126")


# ---------------------------------------------------------------------------
# Module-scoped setup
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module", autouse=True)
def _cluster_ready() -> None:
    """Hard-fail if the daemon isn't reachable.

    The compose healthcheck already gates the `test` service on
    `curl /v2/status → 200`, so by the time this fixture runs the
    listener is up.  The TCP probe on 2126 here is defensive — proves
    the gRPC surface is also alive (the http-api handler forwards to
    it, so a 2126-down cluster would surface as 503 for every search
    request even with a valid bearer, which would mislead the "wrong
    bearer" and "no bearer" assertions below).
    """
    wait_healthy([NODE_GRPC])


@pytest.fixture(scope="module")
def sk_key() -> str:
    """Read the sk-* the daemon entrypoint minted before boot.

    The daemon's entrypoint ran `nexusd-cluster auth mint --subject-type
    user --subject-id admin --admin --name http-api-e2e` OFFLINE (before
    the redb was opened by the daemon) and tee'd its stdout into
    `/shared/sk-key`, mounted read-only on this container via a shared
    docker volume.  A missing / empty file means the mint step failed
    or the compose wiring drifted — fail loud with the actual path so
    an operator running the suite locally can debug.
    """
    path = pathlib.Path(KEY_FILE)
    assert path.is_file(), (
        f"minted sk-* key file {path} is missing — either the daemon's "
        f"entrypoint mint step failed or the compose shared-volume wiring "
        f"drifted; check `docker logs nexus-http-api-node` for the mint output."
    )
    key = path.read_text().strip()
    assert key.startswith("sk-"), (
        f"minted key at {path} does not start with 'sk-' — got {key[:8]!r}. "
        f"The mint subcommand should print the raw key on stdout; a stderr "
        f"log line leaked into the file suggests the entrypoint's shell "
        f"redirection was wrong."
    )
    return key


# ---------------------------------------------------------------------------
# Wire helpers — stdlib only
# ---------------------------------------------------------------------------
def _http_get(url: str, headers: dict[str, str] | None = None) -> tuple[int, dict]:
    """`urllib.request` GET returning (status, parsed_json_body).

    Non-2xx statuses raise `HTTPError`; we catch and read the body from
    the exception so callers can pin error-body JSON shapes.
    """
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"_raw": body}


def _http_post_json(
    url: str, body: dict, headers: dict[str, str] | None = None
) -> tuple[int, dict]:
    """`urllib.request` POST with a JSON body; same return shape as `_http_get`."""
    payload = json.dumps(body).encode("utf-8")
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=payload, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp_body = resp.read().decode("utf-8", errors="replace")
            return resp.status, (json.loads(resp_body) if resp_body else {})
    except urllib.error.HTTPError as e:
        resp_body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(resp_body)
        except json.JSONDecodeError:
            return e.code, {"_raw": resp_body}


# ===========================================================================
# Tests
# ===========================================================================
class TestPublicRoute:
    """`/v2/status` is deliberately outside the bearer middleware."""

    def test_status_is_public_without_auth(self) -> None:
        # No `Authorization` header — a strict `ApiKeyAuthProvider` on a
        # protected route would 401 this.  `/v2/status` lives on the
        # PUBLIC sub-router (see `router()` in `rust/services/http-api/
        # src/lib.rs`) so it bypasses the bearer gate entirely.
        status, body = _http_get(f"{HTTP_URL}/v2/status")
        assert status == 200, f"public status route must 200 without auth; got {status} {body}"
        assert body.get("status") == "ok", f"unexpected status body: {body}"

    def test_status_shape_is_stable(self) -> None:
        # Pins the wire contract of the readiness endpoint — a caller
        # (kube liveness probe, docker healthcheck, operator curl)
        # should see the same fields on every release.
        status, body = _http_get(f"{HTTP_URL}/v2/status")
        assert status == 200
        for required in ("status", "version"):
            assert required in body, f"missing required field {required!r} in {body}"


class TestBearerRejection:
    """`/v2/search/query` is behind `require_bearer` (protected sub-router)."""

    def test_search_query_without_bearer_returns_401(self) -> None:
        # Absent header → parser returns Ok(None) → middleware calls
        # `resolve` with empty token → `ApiKeyAuthProvider` rejects
        # empty → 401.  A NoAuth-wired daemon would 200-through with
        # an admin OperationContext — this test would then fail here,
        # exposing that the composition root did not swap NoAuth for
        # the real provider.
        status, body = _http_post_json(f"{HTTP_URL}/v2/search/query", {"q": "anything"})
        assert status == 401, (
            f"protected route without bearer must 401 under real "
            f"ApiKeyAuthProvider; got {status} {body}"
        )

    def test_search_query_with_wrong_bearer_returns_401(self) -> None:
        # Well-formed bearer that the provider does NOT resolve → 401.
        # Proves the provider actually consults the key store — a stub
        # that admits any well-formed token would 200-through.
        status, body = _http_post_json(
            f"{HTTP_URL}/v2/search/query",
            {"q": "anything"},
            headers={"Authorization": "Bearer sk-this-key-was-never-minted"},
        )
        assert status == 401, (
            f"unresolved bearer must 401 (provider must consult the key "
            f"store, not just check header shape); got {status} {body}"
        )


class TestBearerAdmission:
    """The real minted `sk-*` key must be admitted end-to-end."""

    def test_search_query_with_valid_bearer_is_not_401(self, sk_key: str) -> None:
        # `sk_key` is what `nexusd-cluster auth mint --admin` printed
        # during the daemon's boot entrypoint — a REAL key backed by a
        # REAL redb row.  Middleware calls `ApiKeyAuthProvider::resolve`
        # → provider hashes the token → finds the row → returns an
        # admin `OperationContext` → request forwarded to the upstream
        # search backend.
        #
        # Assertion is NOT-401 (rather than 200) because this compose
        # deliberately does not load the search-plugin cdylib — the
        # upstream `SearchService` is unregistered, so the RPC returns
        # `Unimplemented` (mapped to HTTP 501) or `Unavailable`
        # (mapped to 503).  Both prove the auth path succeeded; the
        # search happy-path is covered by the sibling
        # search-plugin-e2e suite (which runs `--insecure-no-auth`
        # for the same reason — those two axes are tested
        # independently).
        status, body = _http_post_json(
            f"{HTTP_URL}/v2/search/query",
            {"q": "anything"},
            headers={"Authorization": f"Bearer {sk_key}"},
        )
        assert status != 401, (
            f"valid minted sk-* must be admitted; got 401 {body}. "
            f"Either the mint step secret mismatched the daemon "
            f"NEXUS_API_KEY_SECRET (HMAC mismatch) or the daemon booted "
            f"under NoAuth (composition-root wiring regressed)."
        )
        # Also assert not 500 — the shared `grpc_status_to_http` mapper
        # in the http-api handler routes `Ok/Cancelled/Unknown/Internal/
        # DataLoss → 500`; a 500 here would mean the upstream RPC hit
        # one of those codes (unexpected shape).  501 / 503 are the
        # documented no-plugin outcomes.
        assert status in (501, 503, 200), (
            f"expected 501 (Unimplemented — no plugin loaded) / 503 "
            f"(Unavailable) / 200 (plugin loaded — unlikely in this "
            f"suite); got {status} {body}"
        )

    def test_public_status_ignores_bearer_when_present(self, sk_key: str) -> None:
        # Sending a bearer to a public route must not cause it to
        # attempt bearer resolution — a strict provider that 500s on
        # unresolvable tokens (there isn't one wired here, but the
        # contract must be strict) still would not touch a public
        # route.  Belt-and-braces pin that the sub-router split holds.
        status, body = _http_get(
            f"{HTTP_URL}/v2/status",
            headers={"Authorization": f"Bearer {sk_key}"},
        )
        assert status == 200, (
            f"public route with a valid bearer must still 200; got {status} {body}"
        )
