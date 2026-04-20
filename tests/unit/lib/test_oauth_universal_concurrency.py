"""Thread-safety regression test for OAuth authorization-URL construction.

A single ``GoogleOAuthProvider`` instance is shared across every concurrent
``/auth/oauth/google/authorize`` request. An earlier implementation mutated
``self.redirect_uri`` for per-call overrides and restored it in a
``try/finally``; between the mutation and the restoration, a concurrent
request could read the *wrong* redirect URI — at best an ``invalid_grant``
from Google, at worst a code delivered to a different callback target.

This test hammers the provider with many concurrent calls, each passing a
distinct ``redirect_uri``, and asserts that each call sees its own value in
the produced URL AND that the provider's own ``self.redirect_uri`` remains
untouched after the storm.
"""

from __future__ import annotations

import threading
from urllib.parse import parse_qs, urlparse

from nexus.lib.oauth.providers.google import GoogleOAuthProvider


def _make_provider() -> GoogleOAuthProvider:
    return GoogleOAuthProvider(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="https://app.example/default",
        scopes=["openid", "email"],
        provider_name="google",
    )


def _redirect_from(url: str) -> str:
    qs = parse_qs(urlparse(url).query)
    assert qs["redirect_uri"], url
    return qs["redirect_uri"][0]


def test_per_call_redirect_uri_does_not_leak_across_threads() -> None:
    provider = _make_provider()
    n = 64
    results: dict[int, str] = {}
    errors: list[Exception] = []
    start = threading.Event()

    def worker(i: int) -> None:
        start.wait()
        try:
            url = provider.get_authorization_url(
                state=f"s-{i}", redirect_uri=f"https://app.example/cb/{i}"
            )
            results[i] = _redirect_from(url)
        except Exception as e:  # pragma: no cover - assertion below surfaces
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join()

    assert not errors, errors
    for i in range(n):
        assert results[i] == f"https://app.example/cb/{i}", (
            f"worker {i} saw leaked redirect_uri={results[i]!r}"
        )
    # Provider's default must be preserved after all calls.
    assert provider.redirect_uri == "https://app.example/default"


def test_default_redirect_uri_used_when_none_passed() -> None:
    provider = _make_provider()
    url = provider.get_authorization_url(state="s")
    assert _redirect_from(url) == "https://app.example/default"


def test_default_redirect_uri_unchanged_after_override() -> None:
    """A single override must not persist — the next call reverts to default."""
    provider = _make_provider()
    provider.get_authorization_url(state="s1", redirect_uri="https://other/cb")
    url = provider.get_authorization_url(state="s2")
    assert _redirect_from(url) == "https://app.example/default"
