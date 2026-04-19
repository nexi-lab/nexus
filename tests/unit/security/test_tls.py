"""Tests for TLS certificate generation and K3s-style join token (#2694)."""

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
from nexus.security.tls.join_token import (
    generate_join_token,
    parse_join_token,
    verify_password,
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
# Integration: full cert lifecycle
# ---------------------------------------------------------------------------


class TestFullLifecycle:
    def test_generate_save_load_verify(self, tmp_path: Path) -> None:
        """End-to-end: generate certs, save, reload, verify fingerprint."""
        tls_dir = tmp_path / "tls"

        ca_a, ca_key_a = generate_zone_ca("zone-a")
        node_a, node_key_a = generate_node_cert(1, "zone-a", ca_a, ca_key_a)
        save_pem(tls_dir / "ca.pem", ca_a)
        save_pem(tls_dir / "ca-key.pem", ca_key_a, is_private=True)
        save_pem(tls_dir / "node.pem", node_a)
        save_pem(tls_dir / "node-key.pem", node_key_a, is_private=True)

        cfg = ZoneTlsConfig.from_data_dir(tmp_path)
        assert cfg is not None

        reloaded = load_pem_cert(cfg.ca_cert_path)
        assert cert_fingerprint(reloaded) == cert_fingerprint(ca_a)


# ---------------------------------------------------------------------------
# Join Token (#2694)
# ---------------------------------------------------------------------------


class TestJoinToken:
    def test_generate_and_parse_roundtrip(self) -> None:
        ca_cert, _ = generate_zone_ca("cluster")
        token, pw_hash = generate_join_token(ca_cert)
        password, fingerprint = parse_join_token(token)

        assert fingerprint == cert_fingerprint(ca_cert)
        assert verify_password(password, pw_hash)

    def test_token_format(self) -> None:
        ca_cert, _ = generate_zone_ca("cluster")
        token, _ = generate_join_token(ca_cert)
        assert token.startswith("K10")
        assert "::server:SHA256:" in token

    def test_verify_password_rejects_wrong(self) -> None:
        ca_cert, _ = generate_zone_ca("cluster")
        _, pw_hash = generate_join_token(ca_cert)
        assert not verify_password("wrong-password", pw_hash)

    def test_parse_invalid_prefix(self) -> None:
        with pytest.raises(ValueError, match="must start with"):
            parse_join_token("BADPREFIX::server:SHA256:abc")

    def test_parse_missing_separator(self) -> None:
        with pytest.raises(ValueError, match="separator"):
            parse_join_token("K10password_only")

    def test_parse_empty_password(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            parse_join_token("K10::server:SHA256:abc")

    def test_two_tokens_different_passwords(self) -> None:
        ca_cert, _ = generate_zone_ca("cluster")
        token1, hash1 = generate_join_token(ca_cert)
        token2, hash2 = generate_join_token(ca_cert)
        pw1, _ = parse_join_token(token1)
        pw2, _ = parse_join_token(token2)
        assert pw1 != pw2
        assert hash1 != hash2
        # Each password matches its own hash
        assert verify_password(pw1, hash1)
        assert verify_password(pw2, hash2)
        # Cross-verify fails
        assert not verify_password(pw1, hash2)
        assert not verify_password(pw2, hash1)
