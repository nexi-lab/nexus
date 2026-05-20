"""Tests for the shared remote gRPC target resolver (Issue #4132).

`resolve_grpc_target` is the single source of truth used by BOTH
`nexus.connect(profile="remote")` and `nexus doctor remote`, so the
preflight reflects the exact connection behavior the SDK uses.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.remote.grpc_target import resolve_grpc_target


def test_port_precedence_env_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_GRPC_PORT", "9999")
    monkeypatch.delenv("NEXUS_GRPC_TLS", raising=False)
    monkeypatch.delenv("NEXUS_DATA_DIR", raising=False)
    addr, port, tls = resolve_grpc_target("http://hub.example.com:2026")
    assert addr == "hub.example.com:9999"
    assert port == 9999
    assert tls is None  # no TLS signals → insecure


def test_invalid_grpc_port_env_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """A present-but-non-integer NEXUS_GRPC_PORT must raise (not silently
    fall back to 2028 and dial the wrong port)."""
    monkeypatch.setenv("NEXUS_GRPC_PORT", "notaport")
    monkeypatch.delenv("NEXUS_GRPC_TLS", raising=False)
    with pytest.raises(ValueError, match="NEXUS_GRPC_PORT"):
        resolve_grpc_target("http://hub:2026")


def test_out_of_range_grpc_port_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_GRPC_PORT", "70000")
    monkeypatch.delenv("NEXUS_GRPC_TLS", raising=False)
    with pytest.raises(ValueError, match="1.65535"):
        resolve_grpc_target("http://hub:2026")


def test_invalid_nexus_yaml_port_fails_fast(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A present-but-invalid nexus.yaml ports.grpc must also fail fast."""
    monkeypatch.delenv("NEXUS_GRPC_PORT", raising=False)
    monkeypatch.delenv("NEXUS_GRPC_TLS", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "nexus.yaml").write_text("ports:\n  grpc: not-a-number\n")
    with pytest.raises(ValueError, match="nexus.yaml ports.grpc"):
        resolve_grpc_target("http://hub:2026")


def test_explicit_remote_ignores_local_nexus_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Boundary: with trust_local_project=False (explicit remote target
    — `doctor remote --url` / `connect(profile=remote, url=...)`), a cwd
    ./nexus.yaml must NOT poison the remote hub's port/TLS. Local
    `ports.grpc: 3028` must not make `--url http://prod:2026` dial
    `prod:3028`."""
    monkeypatch.delenv("NEXUS_GRPC_PORT", raising=False)
    monkeypatch.delenv("NEXUS_GRPC_TLS", raising=False)
    monkeypatch.delenv("NEXUS_DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "nexus.yaml").write_text("ports:\n  grpc: 3028\ntls: true\n")

    # default (trust_local_project=True) DOES read it — sanity
    addr_local, port_local, _ = resolve_grpc_target("http://prod:2026")
    assert (addr_local, port_local) == ("prod:3028", 3028)

    # explicit remote IGNORES it → default 2028, no TLS from local cfg
    addr, port, tls = resolve_grpc_target("http://prod:2026", trust_local_project=False)
    assert (addr, port) == ("prod:2028", 2028)
    assert tls is None  # local `tls: true` must not apply to the remote


def test_default_port_no_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXUS_GRPC_PORT", raising=False)
    monkeypatch.delenv("NEXUS_GRPC_TLS", raising=False)
    monkeypatch.delenv("NEXUS_DATA_DIR", raising=False)
    addr, port, tls = resolve_grpc_target("http://hub:2026")
    assert addr == "hub:2028"
    assert port == 2028
    assert tls is None


def test_grpc_tls_true_resolves_tls_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """NEXUS_GRPC_TLS=true + a data dir with certs → tls_config is
    resolved (so the preflight uses TLS like the SDK, instead of
    wrongly reporting a TLS hub as insecure)."""
    monkeypatch.setenv("NEXUS_GRPC_TLS", "true")
    monkeypatch.setenv("NEXUS_DATA_DIR", "/some/data")
    monkeypatch.delenv("NEXUS_GRPC_PORT", raising=False)
    sentinel = object()
    with patch(
        "nexus.security.tls.config.ZoneTlsConfig.from_data_dir_any",
        return_value=sentinel,
    ):
        addr, port, tls = resolve_grpc_target("https://hub:443")
    assert tls is sentinel  # SDK-equivalent TLS config, not None


def test_grpc_tls_true_no_certs_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-closed: NEXUS_GRPC_TLS=true but nothing resolves → RuntimeError
    (identical to the SDK; `doctor remote` turns this into an actionable
    ERROR rather than a misleading 'reachable')."""
    monkeypatch.setenv("NEXUS_GRPC_TLS", "true")
    monkeypatch.setenv("NEXUS_DATA_DIR", "/no/certs/here")
    monkeypatch.delenv("NEXUS_TLS_CERT", raising=False)
    with (
        patch(
            "nexus.security.tls.config.ZoneTlsConfig.from_data_dir_any",
            return_value=None,
        ),
        pytest.raises(RuntimeError, match="NEXUS_GRPC_TLS=true"),
    ):
        resolve_grpc_target("https://hub:443")


def test_localhost_pinned_to_ipv4(monkeypatch: pytest.MonkeyPatch) -> None:
    """``localhost`` must resolve to ``127.0.0.1`` so Docker port maps
    (IPv4-only) work on macOS where Happy-Eyeballs picks ``::1`` first.
    """
    monkeypatch.setenv("NEXUS_GRPC_PORT", "2028")
    monkeypatch.delenv("NEXUS_GRPC_TLS", raising=False)
    monkeypatch.delenv("NEXUS_DATA_DIR", raising=False)
    addr, _, _ = resolve_grpc_target("http://localhost:2026")
    assert addr == "127.0.0.1:2028"


def test_ipv6_loopback_pinned_to_ipv4(monkeypatch: pytest.MonkeyPatch) -> None:
    """``::1`` must also pin to ``127.0.0.1`` (Docker host maps bind IPv4)."""
    monkeypatch.setenv("NEXUS_GRPC_PORT", "2028")
    monkeypatch.delenv("NEXUS_GRPC_TLS", raising=False)
    monkeypatch.delenv("NEXUS_DATA_DIR", raising=False)
    addr, _, _ = resolve_grpc_target("http://[::1]:2026")
    assert addr == "127.0.0.1:2028"


def test_non_loopback_host_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real DNS-resolved hosts keep their dual-stack behavior — the
    IPv4 pin must NOT apply outside loopback names."""
    monkeypatch.setenv("NEXUS_GRPC_PORT", "2028")
    monkeypatch.delenv("NEXUS_GRPC_TLS", raising=False)
    monkeypatch.delenv("NEXUS_DATA_DIR", raising=False)
    addr, _, _ = resolve_grpc_target("http://hub.example.com:2026")
    assert addr == "hub.example.com:2028"
    addr2, _, _ = resolve_grpc_target("http://10.0.0.42:2026")
    assert addr2 == "10.0.0.42:2028"
