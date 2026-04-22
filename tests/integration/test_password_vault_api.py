"""Integration tests for /api/v2/password_vault/* endpoints.

Uses the same session-scoped TestClient pattern as test_secrets_api_automated.py:
spin up a real FastAPI app with an in-memory record store, then exercise the
full HTTP + DI + SecretsService + PasswordVaultService stack end-to-end.
"""

from __future__ import annotations

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.server.fastapi_server import create_app
from tests.conftest import make_test_nexus
from tests.helpers.in_memory_record_store import InMemoryRecordStore

# Session-scoped shared server (matches test_secrets_api_automated.py)
_server_app = None
_server_client = None


def setup_server() -> TestClient:
    """Build a server once per session."""
    global _server_app, _server_client

    if _server_app is None:
        import asyncio
        import tempfile

        tmp_path = tempfile.mkdtemp(prefix="nexus_pv_test_")
        in_memory_rs = InMemoryRecordStore()

        loop = asyncio.new_event_loop()
        nexus_fs = loop.run_until_complete(make_test_nexus(tmp_path, record_store=in_memory_rs))
        loop.close()

        api_key = "test-api-key"
        _server_app = create_app(nexus_fs, api_key=api_key)
        _server_client = TestClient(_server_app, raise_server_exceptions=True)
        _server_client.headers["Authorization"] = f"Bearer {api_key}"
        _server_client.headers["X-Actor-ID"] = "test-actor"
        _server_client.headers["X-Zone-ID"] = ROOT_ZONE_ID

    return _server_client


@pytest.fixture(scope="session")
def client() -> TestClient:
    return setup_server()


def _encode(title: str) -> str:
    """URL-encode a title so embedded ``/`` / ``:`` survive routing."""
    return quote(title, safe="")


@pytest.fixture(autouse=True)
def _clean_between_tests(client: TestClient):
    """Delete any leftover vault entries between tests (InMemoryRecordStore is session-shared)."""
    yield
    resp = client.get("/api/v2/password_vault")
    if resp.status_code == 200:
        for entry in resp.json().get("entries", []):
            client.delete(f"/api/v2/password_vault/{_encode(entry['title'])}")


def _sample_body(title: str = "github", **overrides):
    body = {
        "title": title,
        "username": "alice",
        "password": "hunter2",
        "url": "https://github.com",
        "notes": "primary work account",
        "tags": "dev,work",
        "totp_secret": "JBSWY3DPEHPK3PXP",
        "extra": {"recovery_codes": ["a1", "b2"]},
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_endpoints_reject_unauthenticated_requests(client: TestClient) -> None:
    app = client.app
    unauth = TestClient(app)

    for verb, path in [
        ("get", "/api/v2/password_vault"),
        ("get", "/api/v2/password_vault/github"),
        ("put", "/api/v2/password_vault/github"),
        ("delete", "/api/v2/password_vault/github"),
        ("post", "/api/v2/password_vault/github/restore"),
        ("get", "/api/v2/password_vault/github/versions"),
    ]:
        method = getattr(unauth, verb)
        kwargs = {"json": _sample_body()} if verb == "put" else {}
        resp = method(path, **kwargs)
        assert resp.status_code == 401, f"{verb.upper()} {path} -> {resp.status_code}"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_put_then_get_round_trips(client: TestClient) -> None:
    resp = client.put("/api/v2/password_vault/github", json=_sample_body())
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["title"] == "github"
    assert result["version"] == 1

    resp = client.get("/api/v2/password_vault/github")
    assert resp.status_code == 200
    entry = resp.json()
    assert entry["title"] == "github"
    assert entry["username"] == "alice"
    assert entry["password"] == "hunter2"
    assert entry["extra"] == {"recovery_codes": ["a1", "b2"]}


def test_put_bumps_version_and_get_version_query_works(client: TestClient) -> None:
    client.put("/api/v2/password_vault/github", json=_sample_body(password="v1"))
    client.put("/api/v2/password_vault/github", json=_sample_body(password="v2"))

    latest = client.get("/api/v2/password_vault/github").json()
    assert latest["password"] == "v2"

    v1 = client.get("/api/v2/password_vault/github?version=1").json()
    assert v1["password"] == "v1"


def test_put_rejects_mismatched_title(client: TestClient) -> None:
    resp = client.put(
        "/api/v2/password_vault/github",
        json=_sample_body(title="aws"),
    )
    assert resp.status_code == 400
    assert "does not match" in resp.json()["detail"]


def test_get_missing_returns_404(client: TestClient) -> None:
    resp = client.get("/api/v2/password_vault/does-not-exist")
    assert resp.status_code == 404


def test_list_entries_returns_full_payloads(client: TestClient) -> None:
    client.put("/api/v2/password_vault/github", json=_sample_body(title="github"))
    client.put(
        "/api/v2/password_vault/aws",
        json=_sample_body(title="aws", username="root", password="p@ss"),
    )

    resp = client.get("/api/v2/password_vault")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2
    titles = sorted(e["title"] for e in data["entries"])
    assert titles == ["aws", "github"]
    # full payload, not just metadata
    aws = next(e for e in data["entries"] if e["title"] == "aws")
    assert aws["password"] == "p@ss"


def test_delete_then_restore(client: TestClient) -> None:
    client.put("/api/v2/password_vault/github", json=_sample_body())

    resp = client.delete("/api/v2/password_vault/github")
    assert resp.status_code == 200
    assert resp.json() == {"title": "github", "deleted": True}

    # Gone from list + GET
    listed = client.get("/api/v2/password_vault").json()
    assert all(e["title"] != "github" for e in listed["entries"])
    assert client.get("/api/v2/password_vault/github").status_code == 404

    # Restore
    resp = client.post("/api/v2/password_vault/github/restore")
    assert resp.status_code == 200
    assert resp.json() == {"title": "github", "restored": True}
    assert client.get("/api/v2/password_vault/github").status_code == 200


def test_delete_nonexistent_returns_404(client: TestClient) -> None:
    resp = client.delete("/api/v2/password_vault/missing")
    assert resp.status_code == 404


def test_restore_nonexistent_returns_404(client: TestClient) -> None:
    resp = client.post("/api/v2/password_vault/missing/restore")
    assert resp.status_code == 404


def test_list_versions(client: TestClient) -> None:
    client.put("/api/v2/password_vault/github", json=_sample_body(password="v1"))
    client.put("/api/v2/password_vault/github", json=_sample_body(password="v2"))
    client.put("/api/v2/password_vault/github", json=_sample_body(password="v3"))

    resp = client.get("/api/v2/password_vault/github/versions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "github"
    assert data["count"] == 3
    # list_versions returns DESC by version
    assert [v["version"] for v in data["versions"]] == [3, 2, 1]


def test_minimal_entry_just_title(client: TestClient) -> None:
    resp = client.put(
        "/api/v2/password_vault/wifi-home",
        json={"title": "wifi-home"},
    )
    assert resp.status_code == 200

    got = client.get("/api/v2/password_vault/wifi-home").json()
    assert got["title"] == "wifi-home"
    assert got["password"] is None
    assert got["extra"] is None


# ---------------------------------------------------------------------------
# URL-encoding / path regression (real Quip data has URLs as titles)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "https://100moffett.activebuilding.com/portal/wall",
        "a/b/c/d",  # multi-segment
        "weird:chars?and=stuff",  # colons + query-looking chars
    ],
)
def test_url_as_title_round_trips(client: TestClient, title: str) -> None:
    encoded = _encode(title)
    body = _sample_body(title=title, password="secret-for-" + title)

    assert client.put(f"/api/v2/password_vault/{encoded}", json=body).status_code == 200

    got = client.get(f"/api/v2/password_vault/{encoded}").json()
    assert got["title"] == title
    assert got["password"] == "secret-for-" + title

    listed = client.get("/api/v2/password_vault").json()
    assert any(e["title"] == title for e in listed["entries"])

    versions = client.get(f"/api/v2/password_vault/{encoded}/versions").json()
    assert versions["title"] == title and versions["count"] == 1

    assert client.delete(f"/api/v2/password_vault/{encoded}").status_code == 200
    assert client.get(f"/api/v2/password_vault/{encoded}").status_code == 404
    assert client.post(f"/api/v2/password_vault/{encoded}/restore").status_code == 200
    assert client.get(f"/api/v2/password_vault/{encoded}").status_code == 200


def test_title_length_cap_rejects_at_1025_chars(client: TestClient) -> None:
    long_title = "a" * 1025
    resp = client.put(
        f"/api/v2/password_vault/{_encode(long_title)}",
        json={"title": long_title},
    )
    assert resp.status_code == 400
    assert "too long" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# access_context query params (audit observability, Ask 1 of spec v3)
# ---------------------------------------------------------------------------


def test_get_entry_accepts_access_context_query_params(client: TestClient) -> None:
    client.put("/api/v2/password_vault/github", json=_sample_body())

    for ctx in ("admin_cli", "auto_login", "auto_rotate", "reveal_approved", "agent_direct"):
        resp = client.get(
            f"/api/v2/password_vault/github"
            f"?access_context={ctx}&client_id=sudowork&agent_session=s-1"
        )
        assert resp.status_code == 200, f"{ctx}: {resp.status_code} {resp.text}"
        assert resp.json()["title"] == "github"


def test_get_entry_without_access_context_still_works(client: TestClient) -> None:
    """Backwards compat: omitting access_context must behave exactly as before."""
    client.put("/api/v2/password_vault/github", json=_sample_body())

    resp = client.get("/api/v2/password_vault/github")

    assert resp.status_code == 200
    assert resp.json()["title"] == "github"


def test_get_entry_rejects_unknown_access_context(client: TestClient) -> None:
    client.put("/api/v2/password_vault/github", json=_sample_body())

    resp = client.get("/api/v2/password_vault/github?access_context=bogus_value")

    assert resp.status_code == 400
    assert "bogus_value" in resp.json()["detail"]


def test_list_entries_accepts_access_context_query_params(client: TestClient) -> None:
    client.put("/api/v2/password_vault/github", json=_sample_body())

    resp = client.get("/api/v2/password_vault?access_context=auto_login&client_id=sudowork")

    assert resp.status_code == 200
    assert resp.json()["count"] == 1


def test_list_entries_rejects_unknown_access_context(client: TestClient) -> None:
    resp = client.get("/api/v2/password_vault?access_context=not_a_real_value")

    assert resp.status_code == 400


def test_get_entry_with_version_query_and_access_context(client: TestClient) -> None:
    """Query params compose: ?version=N coexists with access_context."""
    client.put("/api/v2/password_vault/github", json=_sample_body(password="v1"))
    client.put("/api/v2/password_vault/github", json=_sample_body(password="v2"))

    resp = client.get("/api/v2/password_vault/github?version=1&access_context=admin_cli")

    assert resp.status_code == 200
    assert resp.json()["password"] == "v1"
