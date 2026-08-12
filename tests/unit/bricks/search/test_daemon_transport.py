"""Transport-security contract for the SearchDaemon proxy channel.

#4622 review R1: the proxy dialed `insecure_channel` unconditionally,
so a secured plugin host was unreachable and the only working
cross-host topology was an unauthenticated one.  These tests pin the
decision logic: plaintext is loopback-only unless explicitly allowed,
and TLS env turns on a secure channel (with optional mTLS identity).
"""

from __future__ import annotations

import sys
import types

if "nexus_runtime" not in sys.modules:
    from unittest.mock import MagicMock as _MagicMock

    _nexus_runtime_stub = _MagicMock()
    _nexus_runtime_stub.__name__ = "nexus_runtime"
    _nexus_runtime_stub.__spec__ = types.ModuleType("nexus_runtime")
    sys.modules["nexus_runtime"] = _nexus_runtime_stub

import pytest

from nexus.bricks.search import daemon as daemon_mod


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        daemon_mod._TLS_ENV,
        daemon_mod._TLS_CA_ENV,
        daemon_mod._TLS_CERT_ENV,
        daemon_mod._TLS_KEY_ENV,
        daemon_mod._ALLOW_INSECURE_ENV,
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def channel_spies(monkeypatch: pytest.MonkeyPatch):
    calls: dict[str, object] = {}

    def fake_insecure(target):
        calls["mode"] = "insecure"
        calls["target"] = target
        return object()

    def fake_secure(target, creds):
        calls["mode"] = "secure"
        calls["target"] = target
        calls["creds"] = creds
        return object()

    monkeypatch.setattr(daemon_mod.grpc.aio, "insecure_channel", fake_insecure)
    monkeypatch.setattr(daemon_mod.grpc.aio, "secure_channel", fake_secure)
    return calls


def test_loopback_plaintext_is_allowed(channel_spies):
    daemon_mod._build_channel("127.0.0.1:2126")
    assert channel_spies["mode"] == "insecure"


@pytest.mark.parametrize("target", ["plugin-host:2126", "10.0.0.7:2126"])
def test_non_loopback_plaintext_refused_by_default(channel_spies, target):
    with pytest.raises(RuntimeError, match="refusing PLAINTEXT"):
        daemon_mod._build_channel(target)
    assert "mode" not in channel_spies


def test_docker_host_gateway_counts_as_same_machine(channel_spies):
    # host.docker.internal resolves to the machine RUNNING the
    # container — same-machine by construction (review R5), so the
    # dev default target needs no blanket insecure opt-in that would
    # also waive protection for genuinely remote hosts.
    daemon_mod._build_channel("host.docker.internal:2126")
    assert channel_spies["mode"] == "insecure"


def test_non_loopback_plaintext_allowed_with_explicit_opt_in(
    channel_spies, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(daemon_mod._ALLOW_INSECURE_ENV, "true")
    daemon_mod._build_channel("plugin-host:2126")
    assert channel_spies["mode"] == "insecure"


def test_tls_env_builds_secure_channel(channel_spies, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(daemon_mod._TLS_ENV, "true")
    daemon_mod._build_channel("plugin-host:2126")
    assert channel_spies["mode"] == "secure"
    assert channel_spies["creds"] is not None


def test_tls_reads_ca_and_client_identity(channel_spies, monkeypatch: pytest.MonkeyPatch, tmp_path):
    ca = tmp_path / "ca.pem"
    cert = tmp_path / "client.pem"
    key = tmp_path / "client-key.pem"
    ca.write_bytes(b"CA")
    cert.write_bytes(b"CERT")
    key.write_bytes(b"KEY")
    captured: dict[str, bytes | None] = {}

    def fake_creds(root_certificates=None, private_key=None, certificate_chain=None):
        captured["ca"] = root_certificates
        captured["key"] = private_key
        captured["cert"] = certificate_chain
        return object()

    monkeypatch.setattr(daemon_mod.grpc, "ssl_channel_credentials", fake_creds)
    monkeypatch.setenv(daemon_mod._TLS_ENV, "true")
    monkeypatch.setenv(daemon_mod._TLS_CA_ENV, str(ca))
    monkeypatch.setenv(daemon_mod._TLS_CERT_ENV, str(cert))
    monkeypatch.setenv(daemon_mod._TLS_KEY_ENV, str(key))

    daemon_mod._build_channel("plugin-host:2126")
    assert channel_spies["mode"] == "secure"
    assert captured == {"ca": b"CA", "key": b"KEY", "cert": b"CERT"}


def test_ipv6_loopback_counts_as_loopback():
    assert daemon_mod._target_is_loopback("[::1]:2126")
    assert daemon_mod._target_is_loopback("localhost:2126")
    assert not daemon_mod._target_is_loopback("plugin-host:2126")


# ── #4628: result mapping carries title-arm attribution ──────────


def test_result_to_base_copies_title_score_when_set() -> None:
    """#4628: title-arm attribution must survive the proto→Base hop —
    the HTTP, batch, and federated serializers all read
    BaseSearchResult.title_score and omit-when-None."""
    from nexus.grpc.search.v1 import search_pb2

    pb = search_pb2.QueryResult(
        path="/designs/atlas.md",
        chunk_index=0,
        chunk_text="body",
        score=0.9,
        zone_id="root",
        title_score=7.0,
    )
    base = daemon_mod._result_to_base(pb)
    assert base.title_score == pytest.approx(7.0)


def test_result_to_base_title_score_none_when_unset() -> None:
    from nexus.grpc.search.v1 import search_pb2

    pb = search_pb2.QueryResult(path="/a.md", chunk_index=0, chunk_text="t", score=0.5)
    base = daemon_mod._result_to_base(pb)
    assert base.title_score is None


# ── #4644: result mapping carries score attribution ──────────────


def test_result_to_base_copies_score_attribution_when_set() -> None:
    """#4644: per-arm scores + boost factors must survive the
    proto→Base hop so the HTTP surface stops serialising null."""
    from nexus.grpc.search.v1 import search_pb2

    pb = search_pb2.QueryResult(
        path="/docs/guide.md",
        chunk_index=1,
        chunk_text="body",
        score=0.42,
        zone_id="root",
        keyword_score=8.25,
        vector_score=0.91,
        tier_boost=2.0,
        recency_boost=1.35,
    )
    base = daemon_mod._result_to_base(pb)
    assert base.keyword_score == pytest.approx(8.25)
    assert base.vector_score == pytest.approx(0.91)
    assert base.tier_boost == pytest.approx(2.0)
    assert base.recency_boost == pytest.approx(1.35)


def test_result_to_base_score_attribution_none_when_unset() -> None:
    """Presence (not zero-ness) decides None — "arm didn't vote" and
    "boost didn't apply" must stay distinct from 0.0."""
    from nexus.grpc.search.v1 import search_pb2

    pb = search_pb2.QueryResult(path="/a.md", chunk_index=0, chunk_text="t", score=0.5)
    base = daemon_mod._result_to_base(pb)
    assert base.keyword_score is None
    assert base.vector_score is None
    assert base.tier_boost is None
    assert base.recency_boost is None
