from __future__ import annotations

import httpx
import pytest

from nexus.services.zones.membership import MembershipUnreachable, MossMembershipVerifier


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_no_cache_revalidates_every_access() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers["Authorization"] == "Bearer secret"
        assert request.url.params["user_id"] == "u1"
        assert request.url.params["org_id"] == "o1"
        return httpx.Response(200, json={"status": "active", "role": "member", "revision": 1})

    verifier = MossMembershipVerifier("http://moss/membership", "secret", client=_client(handler))
    assert verifier.check("u1", "o1", "r1")
    assert verifier.check("u1", "o1", "r1")
    assert calls == 2


def test_worker_cache_reuses_snapshot_within_ttl() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"status": "active", "role": "member", "revision": 1})

    verifier = MossMembershipVerifier(
        "http://moss/membership", "secret", cache_ttl_s=5, client=_client(handler)
    )
    assert verifier.check_detailed("u1", "o1", "r1") == "ok"
    assert verifier.check_detailed("u1", "o1", "r1") == "ok"
    assert calls == 1


@pytest.mark.parametrize(
    ("body", "version"),
    [
        ({"status": "disabled", "role": "member", "revision": 1}, "r1"),
        ({"status": "active", "role": "member", "revision": 2}, "r1"),
    ],
)
def test_inactive_or_advanced_revision_denies(body: dict[str, object], version: str) -> None:
    verifier = MossMembershipVerifier(
        "http://moss/membership",
        "secret",
        client=_client(lambda _request: httpx.Response(200, json=body)),
    )
    assert not verifier.check("u1", "o1", version)
    assert verifier.check_detailed("u1", "o1", version) == "inactive"


def test_unreachable_is_distinct_from_inactive() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    verifier = MossMembershipVerifier("http://moss/membership", "secret", client=_client(handler))
    with pytest.raises(MembershipUnreachable):
        verifier.check("u1", "o1", "r1")
    assert verifier.check_detailed("u1", "o1", "r1") == "unreachable"


def test_missing_membership_is_inactive() -> None:
    verifier = MossMembershipVerifier(
        "http://moss/membership",
        "secret",
        client=_client(lambda _request: httpx.Response(404)),
    )
    assert verifier.check_detailed("missing", "o1", "r1") == "inactive"
