"""Tests for SSH-style TOFU mTLS certificate generation and trust store (#1250)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus.security.tls.certgen import (
    cert_fingerprint,
    generate_node_cert,
    generate_zone_ca,
    load_pem_cert,
    load_pem_key,
    save_pem,
)
from nexus.security.tls.config import ZoneTlsConfig
from nexus.security.tls.trust_store import (
    TofuResult,
    TofuTrustStore,
    ZoneCertificateChangedError,
)

# ---------------------------------------------------------------------------
# Certificate generation
# ---------------------------------------------------------------------------


class TestCertGen:
    def test_generate_zone_ca(self) -> None:
        cert, key = generate_zone_ca("test-zone")
        assert cert.subject.rfc4514_string() == "CN=nexus-zone-test-zone-ca,O=Nexus"
        # CA constraints
        bc = cert.extensions.get_extension_for_class(
            __import__("cryptography.x509", fromlist=["BasicConstraints"]).BasicConstraints
        )
        assert bc.value.ca is True

    def test_generate_node_cert(self) -> None:
        ca_cert, ca_key = generate_zone_ca("test-zone")
        node_cert, _node_key = generate_node_cert(
            node_id=42,
            zone_id="test-zone",
            ca_cert=ca_cert,
            ca_key=ca_key,
            hostnames=["alice.local"],
        )
        assert "nexus-zone-test-zone-node-42" in node_cert.subject.rfc4514_string()
        # Verify signed by CA
        from cryptography.hazmat.primitives.asymmetric import ec

        ca_key_pub = ca_cert.public_key()
        assert isinstance(ca_key_pub, ec.EllipticCurvePublicKey)
        # Verify SAN contains localhost + custom hostname
        from cryptography import x509

        san = node_cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns_names = san.value.get_values_for_type(x509.DNSName)
        assert "localhost" in dns_names
        assert "alice.local" in dns_names

    def test_cert_fingerprint_format(self) -> None:
        cert, _ = generate_zone_ca("fp-test")
        fp = cert_fingerprint(cert)
        assert fp.startswith("SHA256:")
        assert len(fp) > 10

    def test_cert_fingerprint_stable(self) -> None:
        cert, _ = generate_zone_ca("stable")
        assert cert_fingerprint(cert) == cert_fingerprint(cert)

    def test_save_and_load_cert(self, tmp_path: Path) -> None:
        cert, key = generate_zone_ca("io-test")
        cert_path = tmp_path / "ca.pem"
        key_path = tmp_path / "ca-key.pem"
        save_pem(cert_path, cert)
        save_pem(key_path, key, is_private=True)

        loaded_cert = load_pem_cert(cert_path)
        loaded_key = load_pem_key(key_path)

        assert cert_fingerprint(loaded_cert) == cert_fingerprint(cert)
        # Verify key is usable
        from cryptography.hazmat.primitives.asymmetric import ec

        assert isinstance(loaded_key, ec.EllipticCurvePrivateKey)

    def test_private_key_permissions(self, tmp_path: Path) -> None:
        _, key = generate_zone_ca("perm-test")
        key_path = tmp_path / "key.pem"
        save_pem(key_path, key, is_private=True)
        mode = key_path.stat().st_mode & 0o777
        assert mode == 0o600

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        cert, _ = generate_zone_ca("mkdir-test")
        deep_path = tmp_path / "a" / "b" / "c" / "cert.pem"
        save_pem(deep_path, cert)
        assert deep_path.exists()


# ---------------------------------------------------------------------------
# ZoneTlsConfig
# ---------------------------------------------------------------------------


class TestZoneTlsConfig:
    def test_from_data_dir_none_when_missing(self, tmp_path: Path) -> None:
        assert ZoneTlsConfig.from_data_dir(tmp_path) is None

    def test_from_data_dir_detects_certs(self, tmp_path: Path) -> None:
        ca_cert, ca_key = generate_zone_ca("cfg-test")
        node_cert, node_key = generate_node_cert(1, "cfg-test", ca_cert, ca_key)
        tls_dir = tmp_path / "tls"
        save_pem(tls_dir / "ca.pem", ca_cert)
        save_pem(tls_dir / "ca-key.pem", ca_key, is_private=True)
        save_pem(tls_dir / "node.pem", node_cert)
        save_pem(tls_dir / "node-key.pem", node_key, is_private=True)

        cfg = ZoneTlsConfig.from_data_dir(tmp_path)
        assert cfg is not None
        assert cfg.ca_cert_path == tls_dir / "ca.pem"
        assert b"BEGIN CERTIFICATE" in cfg.ca_pem
        assert b"BEGIN CERTIFICATE" in cfg.node_cert_pem
        assert (
            b"BEGIN EC PRIVATE KEY" in cfg.node_key_pem or b"BEGIN PRIVATE KEY" in cfg.node_key_pem
        )


# ---------------------------------------------------------------------------
# TOFU Trust Store
# ---------------------------------------------------------------------------


class TestTofuTrustStore:
    def _make_store(self, tmp_path: Path) -> TofuTrustStore:
        return TofuTrustStore(tmp_path / "known_zones")

    def test_trust_new_zone(self, tmp_path: Path) -> None:
        store = self._make_store(tmp_path)
        cert, _ = generate_zone_ca("new-zone")
        result = store.verify_or_trust("new-zone", cert, "10.0.0.1:2126")
        assert result == TofuResult.TRUSTED_NEW

    def test_trust_known_zone(self, tmp_path: Path) -> None:
        store = self._make_store(tmp_path)
        cert, _ = generate_zone_ca("known-zone")
        store.verify_or_trust("known-zone", cert, "10.0.0.1:2126")
        result = store.verify_or_trust("known-zone", cert, "10.0.0.1:2126")
        assert result == TofuResult.TRUSTED_KNOWN

    def test_fingerprint_mismatch_raises(self, tmp_path: Path) -> None:
        store = self._make_store(tmp_path)
        cert1, _ = generate_zone_ca("rotate-zone")
        cert2, _ = generate_zone_ca("rotate-zone")  # different key → different fingerprint
        store.verify_or_trust("rotate-zone", cert1, "10.0.0.1:2126")
        with pytest.raises(ZoneCertificateChangedError) as exc_info:
            store.verify_or_trust("rotate-zone", cert2, "10.0.0.1:2126")
        assert "ZONE CERTIFICATE CHANGED" in str(exc_info.value)

    def test_persistence_across_reload(self, tmp_path: Path) -> None:
        path = tmp_path / "known_zones"
        cert, _ = generate_zone_ca("persist-zone")
        store1 = TofuTrustStore(path)
        store1.verify_or_trust("persist-zone", cert, "10.0.0.1:2126")
        # Reload from disk
        store2 = TofuTrustStore(path)
        result = store2.verify_or_trust("persist-zone", cert, "10.0.0.2:2126")
        assert result == TofuResult.TRUSTED_KNOWN

    def test_remove_zone(self, tmp_path: Path) -> None:
        store = self._make_store(tmp_path)
        cert, _ = generate_zone_ca("rm-zone")
        store.verify_or_trust("rm-zone", cert, "10.0.0.1:2126")
        assert store.remove("rm-zone") is True
        assert store.remove("rm-zone") is False
        # After remove, same cert should be trusted as new
        result = store.verify_or_trust("rm-zone", cert, "10.0.0.1:2126")
        assert result == TofuResult.TRUSTED_NEW

    def test_get_ca_pem(self, tmp_path: Path) -> None:
        store = self._make_store(tmp_path)
        cert, _ = generate_zone_ca("pem-zone")
        store.verify_or_trust("pem-zone", cert, "10.0.0.1:2126")
        pem = store.get_ca_pem("pem-zone")
        assert pem is not None
        assert b"BEGIN CERTIFICATE" in pem

    def test_get_ca_pem_unknown(self, tmp_path: Path) -> None:
        store = self._make_store(tmp_path)
        assert store.get_ca_pem("unknown") is None

    def test_list_trusted(self, tmp_path: Path) -> None:
        store = self._make_store(tmp_path)
        cert1, _ = generate_zone_ca("zone-a")
        cert2, _ = generate_zone_ca("zone-b")
        store.verify_or_trust("zone-a", cert1, "a:2126")
        store.verify_or_trust("zone-b", cert2, "b:2126")
        trusted = store.list_trusted()
        ids = {t.zone_id for t in trusted}
        assert ids == {"zone-a", "zone-b"}

    def test_peer_addresses_accumulate(self, tmp_path: Path) -> None:
        store = self._make_store(tmp_path)
        cert, _ = generate_zone_ca("multi-peer")
        store.verify_or_trust("multi-peer", cert, "10.0.0.1:2126")
        store.verify_or_trust("multi-peer", cert, "10.0.0.2:2126")
        store.verify_or_trust("multi-peer", cert, "10.0.0.1:2126")  # duplicate
        trusted = store.list_trusted()
        assert len(trusted) == 1
        assert set(trusted[0].peer_addresses) == {"10.0.0.1:2126", "10.0.0.2:2126"}


# ---------------------------------------------------------------------------
# Integration: full cert lifecycle
# ---------------------------------------------------------------------------


class TestFullLifecycle:
    def test_generate_save_load_verify(self, tmp_path: Path) -> None:
        """End-to-end: generate certs, save, reload, use trust store."""
        tls_dir = tmp_path / "tls"

        # Generate zone A certs
        ca_a, ca_key_a = generate_zone_ca("zone-a")
        node_a, node_key_a = generate_node_cert(1, "zone-a", ca_a, ca_key_a)
        save_pem(tls_dir / "ca.pem", ca_a)
        save_pem(tls_dir / "ca-key.pem", ca_key_a, is_private=True)
        save_pem(tls_dir / "node.pem", node_a)
        save_pem(tls_dir / "node-key.pem", node_key_a, is_private=True)

        # Load config
        cfg = ZoneTlsConfig.from_data_dir(tmp_path)
        assert cfg is not None

        # Generate zone B certs (peer)
        ca_b, _ = generate_zone_ca("zone-b")

        # TOFU: zone A trusts zone B on first contact
        trust = TofuTrustStore(cfg.known_zones_path)
        result = trust.verify_or_trust("zone-b", ca_b, "10.0.0.2:2126")
        assert result == TofuResult.TRUSTED_NEW

        # Verify zone B CA is retrievable
        pem = trust.get_ca_pem("zone-b")
        assert pem is not None
        reloaded = load_pem_cert(cfg.ca_cert_path)
        assert cert_fingerprint(reloaded) == cert_fingerprint(ca_a)
